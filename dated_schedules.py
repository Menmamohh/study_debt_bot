"""Dated, immutable sheet revisions. Drive files are inputs, never publications."""
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import database as db


class Conflict(ValueError):
    pass


def backup_before_migration(path):
    """SQLite online backup, including WAL; never copy a live .db file directly."""
    if not os.path.isfile(path):
        return
    with sqlite3.connect(path) as source:
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='dated_migrations'").fetchone():
            if source.execute('SELECT 1 FROM dated_migrations WHERE version=1').fetchone():
                return
        dest = path + '.before-dated-v1.bak'
        if os.path.exists(dest):
            with sqlite3.connect(dest) as target:
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Резервная копия базы повреждена; миграция остановлена.')
            return
        staging = dest + '.tmp'
        with sqlite3.connect(staging) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Не удалось проверить резервную копию; миграция остановлена.')
        os.replace(staging, dest)


def migrate(conn):
    # No DROP/DELETE/ALTER of legacy tables. The marker is committed with import.
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS dated_migrations (version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS dated_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, author TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dated_publications (
            batch_id INTEGER NOT NULL REFERENCES dated_batches(id), schedule_date TEXT NOT NULL,
            PRIMARY KEY(batch_id,schedule_date));
        CREATE TABLE IF NOT EXISTS dated_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_date TEXT NOT NULL,
            position INTEGER NOT NULL, label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dated_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sheet_id INTEGER NOT NULL REFERENCES dated_sheets(id),
            batch_id INTEGER NOT NULL REFERENCES dated_batches(id), scope TEXT NOT NULL,
            photo TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL, source_key TEXT NOT NULL DEFAULT '',
            UNIQUE(sheet_id,batch_id,scope));
        CREATE INDEX IF NOT EXISTS dated_by_date ON dated_sheets(schedule_date,position);
        CREATE INDEX IF NOT EXISTS dated_by_sheet ON dated_revisions(sheet_id,scope,id);
    ''')
    if conn.execute('SELECT 1 FROM dated_migrations WHERE version=1').fetchone():
        return
    # Legacy snapshots remain intact. Only explicit ISO dates in captions qualify.
    slots = {}
    for row in conn.execute('SELECT * FROM schedule_versions ORDER BY id').fetchall():
        payload = json.loads(row['payload'])
        per_date = {}
        for photo in payload.get('photos', []):
            match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', photo.get('caption', ''))
            if not match:
                continue
            try:
                day = valid_date(match[1])
            except ValueError:
                continue
            per_date.setdefault(day, []).append(photo)
        if not per_date:
            continue
        batch = conn.execute('INSERT INTO dated_batches(token,author,created_at) VALUES (?,?,?)',
                             (f"legacy:{row['id']}", 'Архив', row['created_at'])).lastrowid
        for day, photos in per_date.items():
            conn.execute('INSERT INTO dated_publications VALUES (?,?)', (batch, day))
            for i, photo in enumerate(photos):
                key = (day, i)
                if key not in slots:
                    slots[key] = conn.execute('INSERT INTO dated_sheets(schedule_date,position,label) VALUES (?,?,?)',
                        (day, i + 1, photo.get('caption') or f'Лист {i + 1}')).lastrowid
                conn.execute('INSERT INTO dated_revisions(sheet_id,batch_id,scope,photo,body,received_at) VALUES (?,?,?,?,?,?)',
                    (slots[key], batch, row['group_name'], photo['source'], '', row['created_at']))
            for (slot_day, index), sid in slots.items():
                if slot_day == day and index >= len(photos):
                    conn.execute('INSERT INTO dated_revisions(sheet_id,batch_id,scope,received_at) VALUES (?,?,?,?)',
                                 (sid,batch,row['group_name'],row['created_at']))
    review_slots = {}
    for row in conn.execute("SELECT * FROM schedule_reviews WHERE status='published' ORDER BY id").fetchall():
        try:
            day = valid_date(row['schedule_date'])
        except ValueError:
            continue
        key = (day, row['group_name'])
        if key not in review_slots:
            position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM dated_sheets WHERE schedule_date=?', (day,)).fetchone()[0]
            review_slots[key] = conn.execute('INSERT INTO dated_sheets(schedule_date,position,label) VALUES (?,?,?)',
                                            (day,position,'Проверенный текст замен')).lastrowid
        batch = conn.execute('INSERT INTO dated_batches(token,author,created_at) VALUES (?,?,?)',
                             (f"legacy-review:{row['id']}",'Архив',row['created_at'])).lastrowid
        conn.execute('INSERT INTO dated_publications VALUES (?,?)',(batch,day))
        conn.execute('INSERT INTO dated_revisions(sheet_id,batch_id,scope,body,received_at,source_key) VALUES (?,?,?,?,?,?)',
            (review_slots[key],batch,row['group_name'],row['body'],row['created_at'],f"review:{row['id']}"))
    conn.execute('INSERT INTO dated_migrations VALUES (1)')


def valid_date(value):
    return dt.date.fromisoformat(value).isoformat()


def suggest_date(text):
    """An unconfirmed hint. Never infer the effective date from upload time."""
    match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', text or '')
    if match:
        try:
            return valid_date(match[1])
        except ValueError:
            return None
    match = re.search(r'(?<!\d)(\d{1,2})[.\-](\d{1,2})(?:[.\-](\d{2,4}))?(?!\d)', text or '')
    if not match:
        return None
    year = int(match[3]) if match[3] else dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).year
    if year < 100:
        year += 2000
    try:
        return dt.date(year, int(match[2]), int(match[1])).isoformat()
    except ValueError:
        return None


def _rows(conn, day, group, batch=None):
    limit = batch if batch is not None else 2**63 - 1
    # Latest applicable revision wins, while a group patch leaves all other groups intact.
    return [dict(row) for row in conn.execute('''
        SELECT s.*, r.id revision_id,r.batch_id,r.scope,r.photo,r.body,r.received_at,r.source_key
        FROM dated_sheets s JOIN dated_revisions r ON r.sheet_id=s.id
        WHERE s.schedule_date=? AND r.scope IN (?, 'ALL') AND r.batch_id<=?
        AND (r.photo<>'' OR trim(r.body)<>'')
        AND r.id=(SELECT MAX(n.id) FROM dated_revisions n
                  WHERE n.sheet_id=s.id AND n.scope IN (?, 'ALL') AND n.batch_id<=?)
        ORDER BY s.position,s.id''', (day, group, limit, group, limit))]


def sheets(day, group, batch=None):
    with db.get_connection() as conn:
        return _rows(conn, valid_date(day), group, batch)


def dates(group):
    with db.get_connection() as conn:
        return [row[0] for row in conn.execute('''SELECT DISTINCT s.schedule_date
            FROM dated_sheets s JOIN dated_revisions r ON r.sheet_id=s.id
            WHERE r.scope IN (?, 'ALL') ORDER BY s.schedule_date DESC''', (group,))]


def publications(day, group):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute('''SELECT DISTINCT b.* FROM dated_batches b
            JOIN dated_revisions r ON r.batch_id=b.id JOIN dated_sheets s ON s.id=r.sheet_id
            WHERE s.schedule_date=? AND r.scope IN (?, 'ALL') ORDER BY b.id DESC''', (day, group))]


def _heads(conn, sheet_id, groups):
    return {g: conn.execute("SELECT COALESCE(MAX(id),0) FROM dated_revisions WHERE sheet_id=? AND scope IN (?, 'ALL')",
                           (sheet_id, g)).fetchone()[0] for g in groups}


def expected_heads(sheet_id, groups):
    with db.get_connection() as conn:
        return _heads(conn, sheet_id, groups)


def _freeze(path):
    if not path or not os.path.isfile(path):
        return path or ''  # Telegram file_id
    data = Path(path).read_bytes()
    dest = Path(path).parent / 'versions' / (hashlib.sha256(data).hexdigest() + Path(path).suffix)
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    return str(dest)


def drive_candidates():
    with db.get_connection() as conn:
        rows = conn.execute('SELECT * FROM gdrive_schedule_files WHERE local_path IS NOT NULL '
                            'ORDER BY schedule_date DESC,part_number,file_id').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if not os.path.isfile(item['local_path']):
            continue
        photo = _freeze(item['local_path'])
        checksum = hashlib.sha256(Path(photo).read_bytes()).hexdigest()
        key = f"drive:{item['file_id']}:{checksum}"
        with db.get_connection() as conn:
            published = conn.execute('SELECT 1 FROM dated_revisions WHERE source_key=?', (key,)).fetchone()
        result.append({'photo': photo, 'body': '', 'label': item['filename'],
                       'received_at': item['downloaded_at'], 'source_key': key,
                       'suggested_date': item.get('schedule_date'), 'published': bool(published)})
    return result


def publish(token, operations, author):
    """All dates, revisions and notification intents commit atomically, once per token."""
    if not token or not operations:
        raise ValueError('Нет подготовленных листов.')
    prepared = []
    for operation in operations:
        op = dict(operation)
        op['date'] = valid_date(op.get('date', ''))
        if not op.get('date_confirmed'):
            raise ValueError('Подтверди дату каждого листа.')
        groups = op.get('groups', [])
        if not groups or any(g not in db.COLLEGE_GROUPS and g != 'ALL' for g in groups) or ('ALL' in groups and len(groups) != 1):
            raise ValueError('Уточни область изменения каждого листа.')
        if not op.get('photo') and not op.get('body', '').strip():
            raise ValueError('Лист пуст.')
        op['photo'] = _freeze(op.get('photo', ''))
        prepared.append(op)
    import schedule_review
    with db.get_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT id FROM dated_batches WHERE token=?', (token,)).fetchone()
        if existing:
            return {'batch': existing['id'], 'changed': False, 'dates': []}
        for op in prepared:
            if op.get('review_id'):
                review = conn.execute("SELECT * FROM schedule_reviews WHERE id=? AND status='draft'", (op['review_id'],)).fetchone()
                if not review or review['body'] != op.get('body'):
                    raise Conflict('Черновик распознавания изменился или уже обработан. Открой его заново.')
        touched = set()
        for op in prepared:
            if not op.get('sheet_id'):
                continue
            sid = int(op['sheet_id'])
            row = conn.execute('SELECT * FROM dated_sheets WHERE id=?', (sid,)).fetchone()
            if not row or row['schedule_date'] != op['date']:
                raise Conflict('Дата или лист изменились. Подготовь замену заново.')
            expanded = list(db.COLLEGE_GROUPS) + ['ALL'] if op['groups'] == ['ALL'] else op['groups']
            if _heads(conn, sid, expanded) != op.get('expected'):
                raise Conflict('После предпросмотра этот лист изменился. Проверь свежую версию.')
            for group in expanded:
                if (sid, group) in touched:
                    raise Conflict('Один лист выбран для пересекающихся замен дважды.')
                touched.add((sid, group))
        batch = conn.execute('INSERT INTO dated_batches(token,author) VALUES (?,?)', (token, author)).lastrowid
        changed_dates = {}
        for op in prepared:
            sid = op.get('sheet_id')
            if not sid:
                position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM dated_sheets WHERE schedule_date=?',
                                        (op['date'],)).fetchone()[0]
                sid = conn.execute('INSERT INTO dated_sheets(schedule_date,position,label) VALUES (?,?,?)',
                                   (op['date'], position, op.get('label') or f'Лист {position}')).lastrowid
            for group in op['groups']:
                conn.execute('INSERT INTO dated_revisions(sheet_id,batch_id,scope,photo,body,received_at,source_key) '
                             'VALUES (?,?,?,?,?,?,?)',
                             (sid,batch,group,op['photo'],op.get('body',''),op['received_at'],op.get('source_key','')))
                changed_dates.setdefault(op['date'], set()).add(group)
        for op in prepared:
            if op.get('review_id'):
                conn.execute("UPDATE schedule_reviews SET status='published' WHERE id=?", (op['review_id'],))
        for day, groups in changed_dates.items():
            groups = {'ALL'} if 'ALL' in groups else groups
            conn.execute('INSERT INTO dated_publications VALUES (?,?)', (batch, day))
            for group in groups:
                schedule_review.enqueue_delivery_in_transaction(conn, f'dated:{batch}:{day}:{group}',
                    f'📅 Расписание на {day} обновлено. Изменены только подтверждённые листы.',
                    f'dsv:{day}:{group}', None if group == 'ALL' else group,
                    exclude_user_id=int(author) if str(author).isdigit() else None)
        return {'batch': batch, 'changed': True, 'dates': sorted(changed_dates)}
