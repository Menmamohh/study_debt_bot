import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot
import database as db


class SchedulePublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, db.DB_PATH = db.DB_PATH, str(Path(self.tmp.name) / 'test.db')
        db.init_db()
        for uid, group, status in [(10,'ТТ-36','approved'), (11,'ТТ-37','approved'),
                                   (12,'ХК-37','approved'), (13,'ТТ-36','pending')]:
            db.add_user_request(uid, None, 'Student', 'Student', group)
            db.set_user_status(uid, status)
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=99, user_id=99))
        self.cb = NS(data='adm_pub_send_token', from_user=NS(id=99, username='admin'),
                     message=NS(chat=NS(type='private')), answer=AsyncMock())
        self.telegram = NS(send_message=AsyncMock(), send_photo=AsyncMock(), send_media_group=AsyncMock())
        bot.schedule_publication_lock = None
        self.patches = [patch.object(bot, 'is_admin', return_value=True),
                        patch.object(bot, 'show_user_screen', AsyncMock()),
                        patch.object(bot.gdrive_sync, 'preserve_existing_schedule'),
                        patch.object(bot.asyncio, 'sleep', AsyncMock())]
        for p in self.patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        db.DB_PATH = self.old_db
        self.tmp.cleanup()

    async def draft(self, groups=None, all_groups=False):
        await self.state.set_state(bot.Form.waiting_for_schedule_audience)
        await self.state.update_data(schedule_groups=groups or [], schedule_all=all_groups,
            schedule_token='token', schedule_body='New schedule', schedule_photos=['p1', 'p2'])

    async def test_selected_groups_only_album_failure_and_repeat(self):
        await self.draft(['ТТ-36', 'ТТ-37'])
        def send(**kw):
            if kw['chat_id'] == 10: raise RuntimeError('blocked')
        self.telegram.send_media_group.side_effect = send
        await asyncio.gather(*(bot.cb_schedule_publication(self.cb, self.state, self.telegram) for _ in range(2)))
        self.assertEqual({c.kwargs['chat_id'] for c in self.telegram.send_media_group.await_args_list}, {10,11})
        self.assertEqual(self.telegram.send_media_group.await_count, 2)
        self.assertIsNone(db.get_group_schedule('ALL'))
        self.assertIsNone(db.get_group_schedule('ХК-37'))
        self.assertEqual(db.get_group_schedule('ТТ-36')['photo_file_ids'], ['p1','p2'])
        link = self.telegram.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        self.assertEqual(link, 'sched_view_ТТ-37')

    async def test_upload_does_not_publish_or_select_all(self):
        await self.state.update_data(target_group='SELECT')
        msg = NS(from_user=self.cb.from_user, chat=NS(type='private',id=99),
                 text='New schedule', photo=[], caption=None)
        await bot.process_group_schedule_input(msg, self.state, self.telegram)
        data = await self.state.get_data()
        self.assertFalse(data['schedule_all'])
        self.assertEqual(data['schedule_groups'], [])
        self.assertIsNone(db.get_group_schedule('ALL'))
        self.telegram.send_message.assert_not_awaited()

    async def test_empty_audience_and_cancel_cannot_publish(self):
        await self.draft(['С-37'])
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.assertIsNone(db.get_group_schedule('С-37'))
        self.cb.data = 'adm_pub_cancel_token'
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.cb.data = 'adm_pub_send_token'
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.telegram.send_media_group.assert_not_awaited()

    async def test_all_requires_explicit_selection(self):
        await self.draft()
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.telegram.send_media_group.assert_not_awaited()
        self.cb.data = 'adm_pub_all_token'
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.cb.data = 'adm_pub_send_token'
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.assertEqual({c.kwargs['chat_id'] for c in self.telegram.send_media_group.await_args_list},{10,11,12})
        self.assertIsNotNone(db.get_group_schedule('ALL'))

    async def test_access_denied(self):
        await self.draft(['ТТ-36'])
        with patch.object(bot, 'is_admin', return_value=False):
            await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.telegram.send_media_group.assert_not_awaited()

    async def test_group_query_pagination(self):
        self.assertEqual([r['user_id'] for r in db.get_verified_users_by_group('ТТ-36',200,0)], [10])
        self.assertEqual(db.get_verified_users_by_group('ТТ-36',200,1), [])

    async def test_recipients_rechecked_and_identical_publication_not_resent(self):
        await self.draft(['ТТ-36', 'ТТ-37'])
        db.set_user_status(10, 'rejected')
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.assertEqual([c.kwargs['chat_id'] for c in self.telegram.send_media_group.await_args_list], [11])
        self.telegram.send_media_group.reset_mock()
        await self.draft(['ТТ-36', 'ТТ-37'])
        await bot.cb_schedule_publication(self.cb, self.state, self.telegram)
        self.telegram.send_media_group.assert_not_awaited()
