import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import bot
import database as db
import schedule_review as review
import gdrive_sync


class QueueDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name)/'test.db')
        db.init_db()
        self.date = gdrive_sync.get_target_study_date()[0]
        bot.delivery_lock = None
        bot.review_screen_lock = None
        for uid, group in [(10,'ТТ-36'), (20,'ВД-70'), (30,'ТТ-36')]:
            db.add_user_request(uid,'student','Student','Student',group)
            if uid != 30:
                db.set_user_status(uid,'approved')
        self.telegram = NS(send_message=AsyncMock(return_value=NS(message_id=500)),
                           edit_message_text=AsyncMock(), send_photo=AsyncMock(), delete_message=AsyncMock())

    def tearDown(self):
        db.DB_PATH = self.old
        self.temp.cleanup()

    def draft(self, group='ТТ-36', date=None, fingerprint=None):
        with db.get_connection() as conn:
            return conn.execute('INSERT INTO schedule_reviews (group_name,schedule_date,fingerprint,sources,body) VALUES (?,?,?,?,?)',
                (group,(date or self.date).isoformat(),fingerprint or group,'[]','1-2 Дизайн')).lastrowid

    def callback(self, aid, action='publish'):
        return NS(from_user=NS(id=99), message=NS(chat=NS(type='private')), bot=self.telegram,
                  data=f'sr_{action}_{aid}',answer=AsyncMock())

    async def test_many_drafts_use_one_screen_and_next_replaces_it(self):
        ids=[self.draft(f'group-{i}') for i in range(12)]
        with patch.object(bot,'run_gemini_worker',AsyncMock()), patch.object(bot,'ADMIN_ID',99):
            await bot.prepare_and_notify_schedule_reviews(self.telegram, manual=True)
        self.telegram.send_message.assert_awaited_once()
        self.telegram.send_photo.assert_not_awaited()
        self.assertIn('Осталось на проверке: 12', self.telegram.edit_message_text.call_args.kwargs['text'])
        review.finish_review(ids[0],False)
        db.init_db()
        await bot.show_next_schedule_review(self.telegram,99)
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual(self.telegram.edit_message_text.call_args.kwargs['message_id'],500)
        self.assertIn('Осталось на проверке: 11', self.telegram.edit_message_text.call_args.kwargs['text'])

    async def test_publish_group_and_repeat_tap_never_repeat_delivery(self):
        aid=self.draft()
        self.assertTrue(review.finish_review(aid,True))
        self.assertFalse(review.finish_review(aid,True))
        await bot.deliver_schedule_notifications(self.telegram)
        self.telegram.send_message.assert_awaited_once()
        args=self.telegram.send_message.call_args.kwargs
        self.assertEqual(args['chat_id'],10)
        self.assertFalse(args['disable_notification'])
        self.assertEqual(args['reply_markup'].inline_keyboard[0][0].callback_data,f'sr_view_{aid}')
        self.assertIsNotNone(review.approved_schedule('ТТ-36',self.date))

    async def test_failed_delivery_retries_after_restart_without_resending_success(self):
        review.enqueue_delivery('confirmed:1','new','sched_view_ALL')
        self.telegram.send_message.side_effect=[NS(message_id=1),RuntimeError('offline')]
        with patch.object(bot.logger,'exception'):
            self.assertEqual(await bot.deliver_schedule_notifications(self.telegram),(1,1))
        db.init_db()
        self.telegram.send_message.reset_mock(side_effect=True)
        await bot.deliver_schedule_notifications(self.telegram)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(self.telegram.send_message.call_args.kwargs['chat_id'],20)
        self.assertEqual(review.pending_deliveries(),[])

    async def test_new_drive_sync_does_not_notify_students(self):
        result=dict(ok=True,new_downloaded=2,files=[dict(filename='new',drive_timestamp=123)])
        await bot.notify_synced_schedule(self.telegram,result)
        await bot.notify_synced_schedule(self.telegram,result)
        await bot.notify_synced_schedule(self.telegram,dict(ok=True,new_downloaded=0))
        self.telegram.send_message.assert_not_awaited()

    async def test_future_published_text_opens_correct_date_and_default_menu(self):
        tomorrow=self.date+datetime.timedelta(days=1)
        aid=self.draft(date=tomorrow)
        review.finish_review(aid,True)
        with patch.object(bot,'check_user_access',AsyncMock(return_value=(True,''))), \
             patch.object(bot,'show_user_screen',AsyncMock()) as show:
            await bot.cb_published_schedule_review(NS(data=f'sr_view_{aid}',from_user=NS(id=10),answer=AsyncMock()),NS(clear=AsyncMock()))
            self.assertIn(tomorrow.strftime('%d.%m.%Y'),show.call_args.args[1])
            await bot.render_group_schedule_view(NS(),NS(id=10),'ТТ-36')
            self.assertIn(tomorrow.strftime('%d.%m.%Y'),show.call_args.args[1])

    async def test_skip_sends_no_student_notification_and_completes_queue(self):
        aid=self.draft()
        with patch.object(bot,'is_admin',return_value=True), patch.object(bot,'update_review_screen',AsyncMock()) as screen:
            await bot.cb_schedule_review_action(self.callback(aid,'skip'),NS(clear=AsyncMock()))
        self.telegram.send_message.assert_not_awaited()
        self.assertIn('Все черновики обработаны',screen.call_args.args[2])
        self.assertIsNone(review.approved_schedule('ТТ-36',self.date))

    async def test_superseded_publication_not_delivered_and_not_opened(self):
        aid=self.draft()
        review.finish_review(aid,True)
        with db.get_connection() as conn:
            conn.execute("UPDATE schedule_reviews SET status='superseded' WHERE id=?",(aid,))
        await bot.deliver_schedule_notifications(self.telegram)
        self.telegram.send_message.assert_not_awaited()
        with patch.object(bot,'check_user_access',AsyncMock(return_value=(True,''))), \
             patch.object(bot,'show_user_screen',AsyncMock()) as screen:
            await bot.cb_published_schedule_review(NS(data=f'sr_view_{aid}',from_user=NS(id=10),answer=AsyncMock()),NS(clear=AsyncMock()))
            self.assertIn('заменена',screen.call_args.args[1])

    async def test_pending_newer_draft_preserves_old_published_text_for_same_date(self):
        aid=self.draft()
        review.finish_review(aid,True)
        self.draft(fingerprint='new')
        self.assertEqual(review.next_approved_schedule('ТТ-36',self.date)['id'],aid)

    async def test_cancel_edit_clears_state_before_queue(self):
        self.draft()
        state=NS(clear=AsyncMock())
        with patch.object(bot,'is_admin',return_value=True):
            await bot.cb_schedule_review_queue(NS(from_user=NS(id=99),message=NS(chat=NS(type='private')),
                answer=AsyncMock(),bot=self.telegram),state)
        state.clear.assert_awaited_once()

    async def test_parallel_open_creates_only_one_control_message(self):
        self.draft()
        await asyncio.gather(bot.show_next_schedule_review(self.telegram,99),bot.show_next_schedule_review(self.telegram,99))
        self.telegram.send_message.assert_awaited_once()
        self.telegram.edit_message_text.assert_awaited_once()

    async def test_newer_draft_keeps_pending_confirmed_notification(self):
        aid=self.draft()
        review.finish_review(aid,True)
        self.draft(fingerprint='new')
        await bot.deliver_schedule_notifications(self.telegram)
        self.telegram.send_message.assert_awaited_once()
