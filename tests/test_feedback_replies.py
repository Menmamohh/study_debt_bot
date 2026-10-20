import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
import bot
import database as db


class FeedbackRepliesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'test.db')
        db.init_db()
        self.a = db.save_feedback_report(10, '<Alice>', None, 10, 1, 'bug')
        self.b = db.save_feedback_report(20, 'Bob', None, 20, 2, 'bug 2')
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
        self.admin = patch.object(bot, 'is_admin', lambda user: user.id == 1)
        self.admin.start()
        self.screen = patch.object(bot, 'show_user_screen', AsyncMock())
        self.render = self.screen.start()
        self.edit = patch.object(bot, 'safe_edit_message', AsyncMock())
        self.edit_mock = self.edit.start()
        self.telegram = NS(send_message=AsyncMock(return_value=NS(message_id=999)))

    def tearDown(self):
        self.edit.stop(); self.screen.stop(); self.admin.stop()
        db.DB_PATH = self.old
        self.temp.cleanup()

    def cb(self, data, uid=1):
        return NS(data=data, from_user=NS(id=uid), message=NS(chat=NS(type='private')), answer=AsyncMock())

    async def prepare(self, report=None, text='<test>'):
        await bot.cb_feedback_reply_start(self.cb(f'fbr_start_{report or self.a}'), self.state)
        token = (await self.state.get_data())['feedback_reply_token']
        message = NS(text=text, from_user=NS(id=1), chat=NS(type='private'))
        await bot.process_feedback_reply(message, self.state)
        return token

    async def test_preview_delivery_history_and_duplicate(self):
        token = await self.prepare(self.b)
        self.assertIn('&lt;test&gt;', self.render.call_args.args[1])
        self.telegram.send_message.assert_not_called()
        cb = self.cb(f'fbr_send_{token}')
        await asyncio.gather(*[bot.cb_feedback_reply_action(cb, self.state, self.telegram) for _ in range(2)])
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(self.telegram.send_message.call_args.kwargs['chat_id'],20)
        self.assertIsNone(db.get_feedback_report(self.b)['resolved_at'])
        db.init_db()
        self.assertEqual(db.get_feedback_reply_history(self.b)['total'],1)
        self.assertEqual(db.get_feedback_reply_history(self.a)['total'],0)

    async def test_edit_cancel_and_stale_buttons(self):
        token = await self.prepare()
        await bot.cb_feedback_reply_action(self.cb(f'fbr_edit_{token}'), self.state, self.telegram)
        self.assertNotEqual((await self.state.get_data())['feedback_reply_token'],token)
        await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'), self.state, self.telegram)
        fresh = (await self.state.get_data())['feedback_reply_token']
        await bot.cb_feedback_reply_action(self.cb(f'fbr_cancel_{fresh}'), self.state, self.telegram)
        self.assertEqual(await self.state.get_data(),{})
        self.telegram.send_message.assert_not_called()

    async def test_uncertain_delivery_is_not_retried(self):
        token = await self.prepare()
        self.telegram.send_message.side_effect = TimeoutError('timeout')
        with self.assertLogs(bot.logger, level='ERROR'):
            await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'), self.state, self.telegram)
        await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'), self.state, self.telegram)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(db.get_feedback_reply(token,1)['status'],'unknown')
        self.assertEqual(db.get_feedback_reply_history(self.a)['total'],0)

    async def test_blocked_delivery_and_edit(self):
        token = await self.prepare()
        self.telegram.send_message.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=10,text='x'),message='blocked')
        await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'),self.state,self.telegram)
        self.assertEqual(db.get_feedback_reply(token,1)['status'],'failed')
        self.assertEqual(db.get_feedback_reply_history(self.a)['total'],0)
        await bot.cb_feedback_reply_action(self.cb(f'fbr_edit_{token}'),self.state,self.telegram)
        self.assertEqual(await self.state.get_state(),bot.Form.waiting_for_feedback_reply.state)

    async def test_deleted_or_resolved_report(self):
        token=await self.prepare()
        with db.get_connection() as conn:
            conn.execute('DELETE FROM feedback_reports WHERE id=?',(self.a,))
        await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'),self.state,self.telegram)
        self.telegram.send_message.assert_not_called()
        await bot.cb_feedback_reply_start(self.cb(f'fbr_start_{self.a}'),self.state)
        self.assertEqual(await self.state.get_data(),{})

    async def test_other_report_invalidates_draft(self):
        token=await self.prepare(self.a)
        await bot.cb_feedback_reply_start(self.cb(f'fbr_start_{self.b}'),self.state)
        await bot.cb_feedback_reply_action(self.cb(f'fbr_send_{token}'),self.state,self.telegram)
        self.telegram.send_message.assert_not_called()

    async def test_unauthorized_all_handlers(self):
        token=await self.prepare()
        await bot.cb_feedback_reply_start(self.cb(f'fbr_start_{self.b}',99),self.state)
        for action in ['send','edit','cancel']:
            await bot.cb_feedback_reply_action(self.cb(f'fbr_{action}_{token}',99),self.state,self.telegram)
        await bot.cb_feedback_history(self.cb(f'fbh_{self.a}_0',99))
        await bot.process_feedback_reply(NS(text='evil',from_user=NS(id=99),chat=NS(type='private')),self.state)
        self.telegram.send_message.assert_not_called()
        self.assertEqual(db.get_feedback_reply(token,1)['body'],'<test>')

    async def test_history_pagination_and_escaping(self):
        for _ in range(4):
            r=db.create_feedback_reply(self.a,1)
            db.prepare_feedback_reply(r['token'],1,'<answer>')
            db.claim_feedback_reply(r['token'],1)
            db.finish_feedback_reply(r['token'],'sent',1)
        await bot.cb_feedback_history(self.cb(f'fbh_{self.a}_1'))
        self.assertIn('&lt;answer&gt;',self.render.call_args.args[1])
        self.assertIn('(Минск)',self.render.call_args.args[1])
        self.assertEqual(len(db.get_feedback_reply_history(self.a,1)['replies']),1)
        self.assertTrue(any(b.callback_data==f'fbh_{self.a}_0' for row in self.render.call_args.kwargs['reply_markup'].inline_keyboard for b in row))

    async def test_exit_invalidates_draft_in_middleware(self):
        from aiogram.types import CallbackQuery, Message, Chat, User
        token=await self.prepare()
        event=CallbackQuery(id='1',chat_instance='x',from_user=User(id=1,is_bot=False,first_name='Admin'),data='main_menu',message=Message(message_id=1,date=0,chat=Chat(id=1,type='private')))
        await bot.verification_middleware(AsyncMock(),event,{'state':self.state})
        self.assertEqual(await self.state.get_data(),{})
        self.assertEqual(db.get_feedback_reply(token,1)['status'],'cancelled')

if __name__=='__main__': unittest.main()
