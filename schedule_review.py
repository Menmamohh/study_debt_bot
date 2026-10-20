"""Persistent OCR drafts. Only an explicit admin action publishes their text."""
import hashlib
import json
import os
import threading

import database as db
import gdrive_sync

_prepare_lock = threading.Lock()
MAX_BODY = 2800


def get_review(review_id):
    with db.get_connection() as conn:
        row = conn.execute('SELECT * FROM schedule_reviews WHERE id = ?', (review_id,)).fetchone()
        return dict(row) if row else None


def prepare_reviews():
    """Run after sync, including unchanged syncs so failed notifications can retry."""
    with _prepare_lock:
        payload = gdrive_sync.drive_schedule_payload()
        if not payload:
            return
        by_date = {}
        target_date = gdrive_sync.get_target_study_date()[0].isoformat()
        for photo in payload['photos']:
            caption = photo['caption']
            if not caption.startswith('Замены: '):
                continue
            date = caption.split(': ', 1)[1].split(',', 1)[0]
            if date >= target_date and os.path.isfile(photo['source']):
                by_date.setdefault(date, []).append(photo)
        # Include detected groups and groups with actual users. An unreadable
        # sheet still yields an editable empty draft for known student groups.
        user_groups = {item['group_name'] for item in db.get_verified_user_groups()
                       if item['group_name'] in db.COLLEGE_GROUPS}
        for date, sources in by_date.items():
            fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
            groups = set(user_groups)
            for photo in sources:
                path = photo['source']
                groups.update(group for group, _ in gdrive_sync._schedule_group_markers(path, os.path.getmtime(path))[2])
            for group in sorted(groups):
                with db.get_connection() as conn:
                    exists = conn.execute('SELECT 1 FROM schedule_reviews WHERE group_name=? AND schedule_date=? AND fingerprint=?',
                                          (group, date, fingerprint)).fetchone()
                if exists:
                    continue
                chunks = []
                for photo in sources:
                    path = photo['source']
                    text = gdrive_sync._ocr_schedule_group(path, os.path.getmtime(path), group)
                    if text:
                        chunks.append(text)
                body = '\n\n'.join(chunks)
                if len(body) > MAX_BODY:
                    body = ''  # Do not silently publish a truncated timetable.
                with db.get_connection() as conn:
                    conn.execute("UPDATE schedule_reviews SET status='superseded' WHERE group_name=? AND schedule_date=? AND status='draft'",
                                 (group, date))
                    conn.execute('INSERT OR IGNORE INTO schedule_reviews (group_name,schedule_date,fingerprint,sources,body) VALUES (?,?,?,?,?)',
                                 (group, date, fingerprint, json.dumps(sources, ensure_ascii=False), body))


def pending_reviews(unnotified=False):
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM schedule_reviews WHERE status='draft' AND schedule_date >= ? "
                            + ('AND notified=0 ' if unnotified else '') + 'ORDER BY schedule_date,id',
                            (gdrive_sync.get_target_study_date()[0].isoformat(),)).fetchall()
        return [dict(row) for row in rows]


def mark_notified(review_id):
    with db.get_connection() as conn:
        conn.execute('UPDATE schedule_reviews SET notified=1 WHERE id=?', (review_id,))


def edit_review(review_id, body):
    body = body.strip()
    if not body or len(body) > MAX_BODY:
        return False
    with db.get_connection() as conn:
        result = conn.execute("UPDATE schedule_reviews SET body=? WHERE id=? AND status='draft' AND schedule_date>=?",
                              (body, review_id, gdrive_sync.get_target_study_date()[0].isoformat()))
        return result.rowcount == 1


def finish_review(review_id, publish):
    with db.get_connection() as conn:
        result = conn.execute("UPDATE schedule_reviews SET status=? WHERE id=? AND status='draft' AND schedule_date>=? "
                              + ("AND trim(body)<>''" if publish else ''),
                              ('published' if publish else 'skipped', review_id,
                               gdrive_sync.get_target_study_date()[0].isoformat()))
        return result.rowcount == 1


def approved_schedule(group, date):
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM schedule_reviews WHERE group_name=? AND schedule_date=? ORDER BY id DESC LIMIT 1",
                           (group, date.isoformat())).fetchone()
        return dict(row) if row and row['status'] == 'published' else None
