import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
import bot
import database as db
import asyncio


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'test.db')
        db.init_db()
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))

    def tearDown(self):
        db.DB_PATH = self.old
        self.temp.cleanup()

    def cb(self, data='adm_broadcast_send', uid=1):
        return NS(data=data, from_user=NS(id=uid), message=NS(chat=NS(type='private')), answer=AsyncMock())

    async def test_queue_types_per_user_and_restart(self):
        ordinary = db.publish_announcement(1, 2)
        first = db.publish_announcement(1, 3, important=True, body='first')
        second = db.publish_announcement(1, 4, important=True, body='second')
        self.assertEqual(db.get_unread_announcement(10)['id'], ordinary)
        self.assertEqual(db.get_unread_announcement(10, True)['id'], first)
        db.close_important_announcement(10, ordinary)
        self.assertEqual(db.get_unread_announcement(10)['id'], ordinary)
        db.close_important_announcement(10, first)
        db.close_important_announcement(10, first)
        db.init_db()
        self.assertEqual(db.get_unread_announcement(10, True)['id'], second)
        self.assertEqual(db.get_unread_announcement(20, True)['id'], first)
        db.close_important_announcement(10, second)
        self.assertIsNone(db.get_unread_announcement(10, True))

    async def test_old_schema_migrates_without_losing_reads(self):
        # A database with the exact announcement schema used before this change.
        db.DB_PATH = str(Path(self.temp.name) / 'legacy.db')
        with db.get_connection() as conn:
            conn.execute('CREATE TABLE announcements (id INTEGER PRIMARY KEY, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('INSERT INTO announcements (id,source_chat_id,source_message_id) VALUES (1,1,2)')
            conn.commit()
        db.init_db()
        self.assertEqual(db.get_unread_announcement(10)['id'], 1)
        self.assertIsNone(db.get_unread_announcement(10, True))
        db.mark_announcement_read(10, 1)
        db.init_db()
        self.assertIsNone(db.get_unread_announcement(10))

    async def test_important_delivery_and_duplicate_publish(self):
        bot.broadcast_publish_lock = None
        await self.state.update_data(announcement_chat_id=1, announcement_message_id=2,
                                     announcement_important=True, announcement_text='<hello>')
        telegram = NS(send_message=AsyncMock(return_value=NS(message_id=99)))
        with patch.object(bot, 'is_admin', return_value=True), \
             patch.object(bot, 'safe_edit_message', AsyncMock()), \
             patch.object(bot.adb, 'get_verified_users', AsyncMock(return_value=[{'user_id':10}])), \
             patch.object(bot, 'cleanup_user_ui_messages', AsyncMock()):
            await asyncio.gather(*[bot.cb_admin_broadcast_send(self.cb(), self.state, telegram) for _ in range(2)])
        telegram.send_message.assert_awaited_once()
        args = telegram.send_message.call_args.kwargs
        self.assertFalse(args['disable_notification'])
        self.assertIsNone(args['parse_mode'])
        self.assertEqual(args['text'], '<hello>')
        self.assertEqual(args['reply_markup'].inline_keyboard[0][0].text, '✖️ Закрыть')
        self.assertIsNotNone(db.get_unread_announcement(10, True))
        self.assertIsNone(db.get_unread_announcement(10))

    async def test_ordinary_does_not_push(self):
        await self.state.update_data(announcement_chat_id=1, announcement_message_id=2, announcement_text='news')
        telegram = NS(send_message=AsyncMock())
        with patch.object(bot, 'is_admin', return_value=True), patch.object(bot, 'safe_edit_message', AsyncMock()):
            await bot.cb_admin_broadcast_send(self.cb(), self.state, telegram)
        telegram.send_message.assert_not_awaited()
        self.assertIsNotNone(db.get_unread_announcement(10))

    async def test_show_never_marks_read_or_attaches_menu_photo(self):
        aid = db.publish_announcement(1, 2, important=True, body='news')
        telegram = NS(send_message=AsyncMock(return_value=NS(message_id=99)), send_photo=AsyncMock())
        with patch.object(bot, 'cleanup_user_ui_messages', AsyncMock()):
            self.assertTrue(await bot.show_important_announcement(NS(bot=telegram), 10))
        telegram.send_photo.assert_not_awaited()
        self.assertEqual(db.get_unread_announcement(10, True)['id'], aid)

    async def test_close_renders_menu_and_is_per_user(self):
        aid = db.publish_announcement(1, 2, important=True, body='news')
        with patch.object(bot, 'cb_main_menu', AsyncMock()) as menu:
            await bot.cb_important_announcement_close(self.cb(f'important_close_{aid}', 10), self.state)
            menu.assert_awaited_once()
        self.assertIsNone(db.get_unread_announcement(10, True))
        self.assertIsNotNone(db.get_unread_announcement(20, True))

    async def test_both_menu_entry_points_show_important_first(self):
        db.add_user_request(10, 'student', 'Student', 'Student', 'ТТ-36')
        db.set_user_status(10, 'approved')
        user = NS(id=10, username='student', first_name='Student')
        with patch.object(bot, 'is_admin', return_value=False), \
             patch.object(bot, 'show_important_announcement', AsyncMock(return_value=True)) as show, \
             patch.object(bot, 'show_user_screen', AsyncMock()) as screen:
            await bot.cb_main_menu(NS(from_user=user, message=NS(chat=NS(type='private'))), self.state)
            await bot.cmd_start(NS(from_user=user, chat=NS(type='private')), self.state)
            self.assertEqual(show.await_count, 2)
            screen.assert_not_awaited()

    async def test_photo_sends_only_announcement_photo(self):
        telegram = NS(send_photo=AsyncMock(return_value=NS(message_id=99)), send_message=AsyncMock())
        await bot.send_important_announcement(telegram, 10, dict(id=1, body='caption', photo_file_id='photo-id'))
        telegram.send_photo.assert_awaited_once()
        self.assertEqual(telegram.send_photo.call_args.kwargs['photo'], 'photo-id')
        telegram.send_message.assert_not_awaited()

    async def test_failed_push_remains_available_in_menu(self):
        bot.broadcast_publish_lock = None
        await self.state.update_data(announcement_chat_id=1, announcement_message_id=2,
                                     announcement_important=True, announcement_text='news')
        telegram = NS(send_message=AsyncMock(side_effect=RuntimeError('delivery failed')))
        with patch.object(bot, 'is_admin', return_value=True), \
             patch.object(bot, 'safe_edit_message', AsyncMock()), \
             patch.object(bot.adb, 'get_verified_users', AsyncMock(return_value=[{'user_id':10}])), \
             patch.object(bot.logger, 'exception'):
            await bot.cb_admin_broadcast_send(self.cb(), self.state, telegram)
        self.assertIsNotNone(db.get_unread_announcement(10, True))
