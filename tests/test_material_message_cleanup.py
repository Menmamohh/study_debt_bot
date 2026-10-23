"""Material decisions join menu cleanup; pending requests remain available."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import database as db


class MaterialMessageCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'test.db')
        db.init_db()
        self.telegram = NS(send_message=AsyncMock(return_value=NS(message_id=201)),
                           delete_message=AsyncMock())

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    async def decide(self, author=10, approved=True, photo=True, edit_fails=False):
        subject = db.add_subject(author, 'Моделирование')
        task = db.add_task(author, subject, 'Практическая работа')
        material = db.add_material(author, 'student', task, 'method', 'Фото методички')
        db.set_ui_message_id(99, 100)
        db.set_ui_message_id(author, 100)
        message = NS(chat=NS(id=99), message_id=200,
                     caption='Новый материал' if photo else None,
                     text=None if photo else 'Новый материал',
                     photo=['photo'] if photo else None, document=None,
                     edit_caption=AsyncMock(), edit_text=AsyncMock())
        if edit_fails:
            message.edit_caption.side_effect = RuntimeError('edit failed')
        callback = NS(from_user=NS(id=99), message=message, answer=AsyncMock(),
                      data=f"adm_{'appr' if approved else 'rejc'}_{material}")
        handler = bot.cb_admin_approve if approved else bot.cb_admin_reject
        with patch.object(bot, 'is_admin', return_value=True):
            await handler(callback, self.telegram)
        return material

    async def test_self_approval_cleans_both_cards_on_next_navigation(self):
        material = await self.decide(author=99)
        self.assertEqual(db.get_material_by_id(material)['status'], 'approved')
        self.assertEqual(db.get_ui_message_id(99), 100)
        self.assertEqual(db.get_user_ui_message_ids(99), [100, 200, 201])
        self.telegram.delete_message.assert_not_awaited()
        # Database reopening must not lose the notification IDs.
        db.init_db()
        await bot.cleanup_user_ui_messages(self.telegram, 99, keep_message_id=100)
        self.assertEqual([c.kwargs for c in self.telegram.delete_message.await_args_list],
                         [dict(chat_id=99, message_id=200), dict(chat_id=99, message_id=201)])
        self.assertEqual(db.get_user_ui_message_ids(99), [100])

    async def test_author_and_admin_are_cleaned_in_their_own_chats(self):
        await self.decide()
        self.assertEqual(db.get_user_ui_message_ids(99), [100, 200])
        self.assertEqual(db.get_user_ui_message_ids(10), [100, 201])
        await bot.cleanup_user_ui_messages(self.telegram, 10, keep_message_id=100)
        self.telegram.delete_message.assert_awaited_once_with(chat_id=10, message_id=201)
        self.assertEqual(db.get_user_ui_message_ids(99), [100, 200])

    async def test_rejected_text_material_uses_same_cleanup(self):
        material = await self.decide(approved=False, photo=False)
        self.assertEqual(db.get_material_by_id(material)['status'], 'rejected')
        self.assertEqual(db.get_user_ui_message_ids(99), [100, 200])
        self.assertEqual(db.get_user_ui_message_ids(10), [100, 201])

    async def test_edit_failure_does_not_leave_card_untracked(self):
        await self.decide(edit_fails=True)
        self.assertIn(200, db.get_user_ui_message_ids(99))

    async def test_failed_notification_does_not_track_nonexistent_message(self):
        self.telegram.send_message.side_effect = RuntimeError('unavailable')
        await self.decide()
        self.assertEqual(db.get_user_ui_message_ids(10), [100])
        self.assertEqual(db.get_user_ui_message_ids(99), [100, 200])

    async def test_tracking_keeps_menu_and_is_idempotent(self):
        db.set_ui_message_id(10, 100)
        db.track_ui_message_id(10, 201)
        db.track_ui_message_id(10, 201)
        self.assertEqual(db.get_ui_message_id(10), 100)
        self.assertEqual(db.get_user_ui_message_ids(10), [100, 201])
