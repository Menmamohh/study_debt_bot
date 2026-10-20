import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database as db
import gdrive_sync
import schedule_review as review


class ScheduleReviewTests(unittest.TestCase):
    def setUp(self):
        self.old_db = db.DB_PATH
        self.folder = Path(tempfile.mkdtemp(prefix='schedule-review-'))
        db.DB_PATH = str(self.folder / 'test.db')
        db.init_db()
        self.date = gdrive_sync.get_target_study_date()[0]
        source = self.folder / 'sheet.jpg'
        source.write_bytes(b'fixture')
        self.sources = [{'source': str(source), 'caption': f'Замены: {self.date.isoformat()}, часть 1'}]

    def tearDown(self):
        db.DB_PATH = self.old_db

    def prepare(self, text='1. Дизайн, кабинет 204'):
        with patch.object(gdrive_sync, 'drive_schedule_payload', return_value={'photos': self.sources}), \
             patch.object(gdrive_sync, '_schedule_group_markers', return_value=(100, 100, (('ТТ-36', 0),))), \
             patch.object(gdrive_sync, '_ocr_schedule_group', return_value=text):
            review.prepare_reviews()
        return review.pending_reviews()[0]

    def test_draft_is_private_until_publication_and_survives_restart(self):
        draft = self.prepare()
        self.assertIsNone(review.approved_schedule('ТТ-36', self.date))
        db.init_db()
        self.assertEqual(review.get_review(draft['id'])['body'], draft['body'])
        self.assertTrue(review.edit_review(draft['id'], '2. Технология, кабинет 205'))
        self.assertTrue(review.finish_review(draft['id'], True))
        self.assertFalse(review.finish_review(draft['id'], True))
        self.assertFalse(review.edit_review(draft['id'], 'поздняя правка'))
        self.assertEqual(review.approved_schedule('ТТ-36', self.date)['body'], '2. Технология, кабинет 205')
        self.assertIsNone(review.approved_schedule('ХК-37', self.date))
        self.assertIsNone(review.approved_schedule('ТТ-36', self.date + datetime.timedelta(days=1)))

    def test_empty_requires_edit_and_skipped_is_not_recreated(self):
        draft = self.prepare('')
        self.assertFalse(review.finish_review(draft['id'], True))
        self.assertFalse(review.edit_review(draft['id'], ' '))
        self.assertTrue(review.finish_review(draft['id'], False))
        with patch.object(gdrive_sync, 'drive_schedule_payload', return_value={'photos': self.sources}), \
             patch.object(gdrive_sync, '_schedule_group_markers', return_value=(100, 100, (('ТТ-36', 0),))):
            review.prepare_reviews()
        self.assertEqual(review.pending_reviews(), [])

    def test_updated_source_invalidates_old_buttons_and_notifications_retry(self):
        first = self.prepare()
        self.assertEqual(len(review.pending_reviews(True)), 1)
        review.mark_notified(first['id'])
        self.assertEqual(review.pending_reviews(True), [])
        source = self.folder / 'new-sheet.jpg'
        source.write_bytes(b'new version')
        self.sources[0]['source'] = str(source)
        latest = self.prepare('Исправленный лист')
        self.assertNotEqual(first['id'], latest['id'])
        self.assertFalse(review.finish_review(first['id'], True))
        self.assertFalse(review.edit_review(first['id'], 'old'))
        self.assertEqual(len(review.pending_reviews(True)), 1)

    def test_expired_draft_cannot_publish(self):
        draft = self.prepare()
        with db.get_connection() as conn:
            conn.execute("UPDATE schedule_reviews SET schedule_date='2000-01-01'")
        self.assertFalse(review.finish_review(draft['id'], True))
        self.assertEqual(review.pending_reviews(), [])

    def test_admin_permission_and_empty_publish(self):
        draft = self.prepare('')
        callback = SimpleNamespace(from_user=SimpleNamespace(id=10),
                                   message=SimpleNamespace(chat=SimpleNamespace(type='private')),
                                   answer=AsyncMock(), data=f"sr_publish_{draft['id']}")
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(bot, 'is_admin', return_value=False):
            asyncio.run(bot.cb_schedule_review_action(callback, state))
        self.assertEqual(review.get_review(draft['id'])['status'], 'draft')
        with patch.object(bot, 'is_admin', return_value=True):
            asyncio.run(bot.cb_schedule_review_action(callback, state))
        self.assertEqual(review.get_review(draft['id'])['status'], 'draft')
        self.assertIn('Сначала введи', callback.answer.call_args.args[0])

    def test_student_view_uses_only_approved_date_and_escapes_text(self):
        draft = self.prepare('<текст>')
        review.finish_review(draft['id'], True)
        with patch.object(bot, 'show_user_screen', AsyncMock()) as show:
            asyncio.run(bot.render_group_schedule_view(object(), SimpleNamespace(id=10), 'ТТ-36'))
        self.assertIn('&lt;текст&gt;', show.call_args.args[1])
        self.assertIn('проверен', show.call_args.args[1])

    def test_new_sheet_hides_previously_approved_text_until_reviewed(self):
        first = self.prepare('старые замены')
        review.finish_review(first['id'], True)
        source = self.folder / 'revised.jpg'
        source.write_bytes(b'revised')
        self.sources[0]['source'] = str(source)
        second = self.prepare('новые замены')
        self.assertIsNone(review.approved_schedule('ТТ-36', self.date))
        review.finish_review(second['id'], True)
        self.assertEqual(review.approved_schedule('ТТ-36', self.date)['body'], 'новые замены')

    def test_unreadable_group_with_students_gets_editable_draft(self):
        with patch.object(gdrive_sync, 'drive_schedule_payload', return_value={'photos': self.sources}), \
             patch.object(gdrive_sync, '_schedule_group_markers', return_value=(0, 0, ())), \
             patch.object(gdrive_sync, '_ocr_schedule_group', return_value=''), \
             patch.object(db, 'get_verified_user_groups', return_value=[{'group_name': 'ТТ-36'}]):
            review.prepare_reviews()
        draft = review.pending_reviews()[0]
        self.assertEqual(draft['body'], '')
        self.assertTrue(review.edit_review(draft['id'], 'Замены введены вручную'))
        self.assertTrue(review.finish_review(draft['id'], True))

    def test_publication_notifies_once(self):
        draft = self.prepare()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=10),
                                   message=SimpleNamespace(chat=SimpleNamespace(type='private'),
                                                           edit_reply_markup=AsyncMock(), answer=AsyncMock()),
                                   answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()),
                                   data=f"sr_publish_{draft['id']}")
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(bot, 'is_admin', return_value=True), \
             patch.object(bot.adb, 'get_verified_users_by_group', AsyncMock(return_value=[{'user_id': 123}])):
            asyncio.run(bot.cb_schedule_review_action(callback, state))
            asyncio.run(bot.cb_schedule_review_action(callback, state))
        self.assertEqual(callback.bot.send_message.await_count, 1)
