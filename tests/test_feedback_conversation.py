import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Message, Chat, User
import bot
import database as db


class FeedbackConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=db.DB_PATH
        db.DB_PATH=str(Path(self.temp.name)/'test.db')
        db.init_db()
        db.set_admin_id(1)
        self.report=db.save_feedback_report(10,'Student','ТТ-36',10,1,'bug')
        self.other=db.save_feedback_report(20,'Other','ВД-70',20,1,'other bug')
        self.state=FSMContext(MemoryStorage(),StorageKey(bot_id=1,chat_id=10,user_id=10))
        self.render=patch.object(bot,'show_user_screen',AsyncMock()).start()
        self.isadmin=patch.object(bot,'is_admin',lambda user:user.id==1)
        self.isadmin.start()
        self.telegram=NS(send_message=AsyncMock(return_value=NS(message_id=99)),send_photo=AsyncMock(return_value=NS(message_id=100)))

    def tearDown(self):
        patch.stopall()
        db.DB_PATH=self.old
        self.temp.cleanup()

    def admin_reply(self, report=None):
        r=db.create_feedback_reply(report or self.report,1)
        db.prepare_feedback_reply(r['token'],1,'answer <admin>')
        db.claim_feedback_reply(r['token'],1)
        db.finish_feedback_reply(r['token'],'sent',99)
        return r

    def cb(self, report=None, uid=10, action='start',chat='private'):
        return NS(data=f'fsr_{action}_{report or self.report}',from_user=NS(id=uid),message=NS(chat=NS(type=chat)),answer=AsyncMock())

    def message(self, text='reply <student>',photo=None,mid=2,uid=10):
        return NS(text=text,caption=None,photo=photo,from_user=NS(id=uid),chat=NS(id=uid,type='private'),message_id=mid)

    async def begin(self):
        self.admin_reply()
        await bot.cb_student_feedback_reply(self.cb(),self.state)

    async def test_text_reply_stays_in_original_report_and_notifies_admin(self):
        await self.begin()
        await bot.process_student_feedback_reply(self.message(),self.state,self.telegram)
        self.assertEqual(db.count_open_feedback_reports(),2)
        h=db.get_feedback_reply_history(self.report)
        self.assertEqual(h['total'],2)
        self.assertEqual({r['author'] for r in h['replies']},{'student','admin'})
        self.telegram.send_message.assert_awaited_once()
        args=self.telegram.send_message.call_args.kwargs
        self.assertEqual(args['chat_id'],1)
        self.assertEqual(args['reply_markup'].inline_keyboard[0][0].callback_data,f'adm_feedback_{self.report}')
        self.assertIsNone(db.get_feedback_report(self.report)['resolved_at'])
        self.assertEqual(await self.state.get_data(),{})
        db.init_db()
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],2)

    async def test_photo_without_caption_is_stored_and_viewed_by_admin(self):
        await self.begin()
        await bot.process_student_feedback_reply(self.message(text=None,photo=[NS(file_id='file-small'),NS(file_id='file-large')]),self.state,self.telegram)
        history=db.get_feedback_reply_history(self.report)
        student=next(r for r in history['replies'] if r['author']=='student')
        self.assertEqual(student['photo_file_id'],'file-large')
        await bot.show_feedback_detail(NS(),self.report)
        self.assertIn('Студент',self.render.call_args.args[1])
        buttons=[b.callback_data for row in self.render.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(f"fbphoto_{student['message_id']}",buttons)
        cb=NS(data=f"fbphoto_{student['message_id']}",from_user=NS(id=1),message=NS(chat=NS(type='private')),answer=AsyncMock())
        await bot.cb_feedback_student_photo(cb,self.telegram)
        self.assertEqual(self.telegram.send_photo.call_args.kwargs['photo'],'file-large')

    async def test_student_cannot_reply_to_another_report_or_view_photo(self):
        self.admin_reply(self.other)
        await bot.cb_student_feedback_reply(self.cb(self.other),self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(db.save_student_feedback_reply(self.other,10,10,2,'evil'),'closed')
        await bot.cb_feedback_student_photo(NS(data='fbphoto_1',from_user=NS(id=10),message=NS(chat=NS(type='private')),answer=AsyncMock()),self.telegram)
        self.telegram.send_photo.assert_not_awaited()

    async def test_closed_report_rejects_stale_button_and_inflight_message(self):
        await self.begin()
        db.resolve_feedback_report(self.report)
        await bot.process_student_feedback_reply(self.message(),self.state,self.telegram)
        await bot.cb_student_feedback_reply(self.cb(),self.state)
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],1)
        self.telegram.send_message.assert_not_awaited()

    async def test_cancel_and_main_menu_send_no_reply(self):
        await self.begin()
        await bot.cb_student_feedback_reply(self.cb(action='cancel'),self.state)
        self.assertEqual(await self.state.get_data(),{})
        await bot.cb_student_feedback_reply(self.cb(),self.state)
        with patch.object(bot,'cmd_start',AsyncMock()) as start:
            await bot.process_student_feedback_reply(self.message('/start'),self.state,self.telegram)
            start.assert_awaited_once()
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],1)

    async def test_duplicate_update_is_saved_and_notified_once(self):
        await self.begin()
        await asyncio.gather(*[bot.process_student_feedback_reply(self.message(),self.state,self.telegram) for _ in range(2)])
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],2)
        self.telegram.send_message.assert_awaited_once()

    async def test_invalid_message_does_not_end_composition(self):
        await self.begin()
        await bot.process_student_feedback_reply(self.message(text=None),self.state,self.telegram)
        await bot.process_student_feedback_reply(self.message(text='x'*3501),self.state,self.telegram)
        self.assertEqual(await self.state.get_state(),bot.Form.waiting_for_student_feedback_reply.state)
        self.telegram.send_message.assert_not_awaited()

    async def test_admin_alert_failure_does_not_lose_student_reply(self):
        await self.begin()
        self.telegram.send_message.side_effect=RuntimeError('offline')
        with self.assertLogs(bot.logger,level='ERROR'):
            await bot.process_student_feedback_reply(self.message(),self.state,self.telegram)
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],2)
        self.assertIn('сохранён',self.render.call_args.args[1])

    async def test_admin_answer_contains_reply_button(self):
        r=db.create_feedback_reply(self.report,1)
        db.prepare_feedback_reply(r['token'],1,'answer')
        await self.state.update_data(feedback_reply_token=r['token'])
        await self.state.set_state(bot.Form.waiting_for_feedback_reply_preview)
        cb=NS(data=f"fbr_send_{r['token']}",from_user=NS(id=1),message=NS(chat=NS(type='private')),answer=AsyncMock())
        await bot.cb_feedback_reply_action(cb,self.state,self.telegram)
        args=self.telegram.send_message.call_args.kwargs
        self.assertEqual(args['reply_markup'].inline_keyboard[0][0].callback_data,f'fsr_start_{self.report}')

    async def test_pending_user_can_use_own_support_thread_through_middleware(self):
        self.admin_reply()
        user=User(id=10,is_bot=False,first_name='Student')
        event=CallbackQuery(id='1',chat_instance='x',from_user=user,data=f'fsr_start_{self.report}',
            message=Message(message_id=99,date=0,chat=Chat(id=10,type='private')))
        handler=AsyncMock()
        await bot.verification_middleware(handler,event,{'state':self.state})
        handler.assert_awaited_once()
        await self.state.set_state(bot.Form.waiting_for_student_feedback_reply)
        handler.reset_mock()
        message=Message(message_id=2,date=0,chat=Chat(id=10,type='private'),from_user=user,text='reply')
        await bot.verification_middleware(handler,message,{'state':self.state})
        handler.assert_awaited_once()

    async def test_reply_button_without_delivered_admin_answer_is_rejected(self):
        await bot.cb_student_feedback_reply(self.cb(),self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(db.save_student_feedback_reply(self.report,10,10,2,'reply'),'closed')

    async def test_history_pagination_keeps_both_authors_and_escapes_text(self):
        self.admin_reply()
        for mid in range(2,6):
            db.save_student_feedback_reply(self.report,10,10,mid,'<student>')
        await bot.show_feedback_detail(NS(),self.report)
        self.assertIn('&lt;student&gt;',self.render.call_args.args[1])
        self.assertEqual(db.get_feedback_reply_history(self.report)['total'],5)
        self.assertEqual(len(db.get_feedback_reply_history(self.report,1)['replies']),2)
        self.assertEqual(db.get_feedback_reply_history(self.other)['total'],0)
