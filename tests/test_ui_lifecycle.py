"""End-to-end private UI transitions against isolated SQLite; Telegram is mocked."""
import asyncio
import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import database as db
import schedule_review as review


class UILifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / 'test.db')
        db.init_db()
        self.user = NS(id=10)
        db.add_user_request(10, 'test', 'Test', 'Test', 'ТТ-36')
        db.set_user_status(10, 'approved')
        self.tg = NS(send_message=AsyncMock(return_value=NS(message_id=700)),
            send_photo=AsyncMock(return_value=NS(message_id=600)),
            send_media_group=AsyncMock(return_value=[NS(message_id=601), NS(message_id=602)]),
            delete_message=AsyncMock(), edit_message_text=AsyncMock())
        self.cb = NS(from_user=self.user, data='', bot=self.tg, answer=AsyncMock(),
            message=NS(message_id=500, chat=NS(id=10, type='private'), photo=None,
                edit_text=AsyncMock(), edit_caption=AsyncMock(), delete=AsyncMock()))
        bot.review_screen_lock = None
        bot.schedule_prepare_lock = None
        bot.delivery_lock = None
        db.set_ui_message_id(10, 100)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def deleted(self):
        return [c.kwargs['message_id'] for c in self.tg.delete_message.await_args_list]

    async def test_notification_open_album_then_menu_cleans_every_previous_message(self):
        review.enqueue_delivery('new', 'New sheets', 'sched_view_ALL')
        self.tg.send_message.return_value = NS(message_id=500)
        await bot.deliver_schedule_notifications(self.tg)
        self.assertIn(500, db.get_user_ui_message_ids(10))
        self.tg.send_message.return_value = NS(message_id=700)
        version = dict(id=1, created_at='2026-10-24T07:00:00', payload=dict(photos=[dict(source='p1'), dict(source='p2')]))
        with patch.object(bot, 'is_admin', return_value=False):
            await bot.render_schedule_version(self.cb, self.user, 'ALL', [version], version, is_callback=True)
        self.assertEqual(self.deleted(), [100, 500])
        self.assertEqual(db.get_user_ui_message_ids(10), [601, 602, 700])
        self.cb.message.message_id = 700
        await bot.show_user_screen(self.cb, 'Menu')
        self.assertEqual(db.get_user_ui_message_ids(10), [700])
        self.assertIn(601, self.deleted())
        self.assertIn(602, self.deleted())

    async def test_old_untracked_notification_is_removed_by_source_callback(self):
        version = dict(id=1, created_at='2026-10-24T07:00:00', payload=dict(photos=[dict(source='p')]))
        with patch.object(bot, 'is_admin', return_value=False):
            await bot.render_schedule_version(self.cb, self.user, 'ALL', [version], version, is_callback=True)
        self.assertIn(500, self.deleted())

    async def test_legacy_review_and_photo_disappear_when_leaving_queue(self):
        review.screen_id(10, 'queue', 800)
        review.screen_id(10, 'photo', 801)
        self.cb.message.message_id = 100
        await bot.show_user_screen(self.cb, 'Admin menu')
        self.assertEqual(db.get_user_ui_message_ids(10), [100])
        self.assertIsNone(review.screen_id(10))
        self.assertIsNone(review.screen_id(10, 'photo'))
        self.assertEqual(self.deleted(), [800, 801])

    async def test_review_reuses_current_menu_and_does_not_reappear_after_navigation(self):
        await bot.update_review_screen(self.tg, 10, 'Queue', None)
        self.assertEqual(review.screen_id(10), 100)
        self.tg.send_message.assert_not_awaited()
        self.cb.message.message_id = 100
        await bot.show_user_screen(self.cb, 'Admin menu')
        self.assertIsNone(review.screen_id(10))

    async def test_preview_companions_survive_preview_then_are_cleaned_on_exit(self):
        db.track_ui_message_id(10, 201)
        db.track_ui_message_id(10, 202)
        self.cb.message.message_id = 100
        await bot.show_user_screen(self.cb, 'Preview', keep_message_ids=[201, 202])
        self.assertEqual(db.get_user_ui_message_ids(10), [100, 201, 202])
        await bot.show_user_screen(self.cb, 'Cancelled')
        self.assertEqual(db.get_user_ui_message_ids(10), [100])

    async def test_failed_replacement_keeps_previous_screen(self):
        self.cb.message.message_id = 100
        self.cb.message.edit_text.side_effect = RuntimeError('edit failed')
        self.tg.send_message.side_effect = RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            await bot.show_user_screen(self.cb, 'New screen')
        self.cb.message.delete.assert_not_awaited()
        self.tg.delete_message.assert_not_awaited()
        self.assertEqual(db.get_ui_message_id(10), 100)

    async def test_partial_album_stays_tracked_after_control_message_fails(self):
        self.tg.send_message.side_effect = RuntimeError('send failed')
        version = dict(id=1, created_at='2026-10-24T07:00:00', payload=dict(photos=[dict(source='p')]))
        with patch.object(bot, 'is_admin', return_value=False), self.assertRaises(RuntimeError):
            await bot.render_schedule_version(self.cb, self.user, 'ALL', [version], version, is_callback=True)
        self.assertEqual(db.get_user_ui_message_ids(10), [100, 600])
        self.tg.delete_message.assert_not_awaited()

    async def test_downloaded_material_replaces_list_then_leaves_no_extra_photo(self):
        material = dict(status='approved', task_id=1, material_type='method', title='Guide', user_name='Test', file_type='photo', file_id='p')
        self.cb.data = 'dl_mat_1'
        self.cb.message.answer_photo = AsyncMock(return_value=NS(message_id=600))
        with patch.object(bot.adb, 'get_material_by_id', AsyncMock(return_value=material)), patch.object(bot.adb, 'get_task_info', AsyncMock(return_value=None)), patch.object(bot, 'is_admin', return_value=False):
            await bot.cb_download_material(self.cb)
        self.assertEqual(db.get_user_ui_message_ids(10), [600])
        self.cb.message.message_id = 600
        await bot.show_user_screen(self.cb, 'Back')
        self.assertEqual(db.get_user_ui_message_ids(10), [600])

    async def test_general_sheet_album_is_current_and_cleans_on_back(self):
        with patch.object(bot.adb, 'get_group_schedule', AsyncMock(return_value=None)), patch.object(bot, 'get_general_schedule_photo', return_value=['p1','p2']), patch.object(bot, 'build_general_schedule_media', return_value=[]):
            await bot.cb_schedule_view_gen_photo(self.cb)
        self.assertEqual(db.get_user_ui_message_ids(10), [601,602,700])
        self.assertIn(500, self.deleted())
        self.cb.message.message_id = 700
        await bot.show_user_screen(self.cb, 'Back')
        self.assertEqual(db.get_user_ui_message_ids(10), [700])

    async def test_late_gemini_result_does_not_replace_menu_even_when_id_is_same(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def worker(*args):
            started.set()
            await release.wait()
        with patch.object(bot, 'ADMIN_ID', 10), patch.object(bot, 'run_gemini_worker', side_effect=worker), patch.object(bot, 'show_next_schedule_review', AsyncMock()) as show:
            task = asyncio.create_task(bot.prepare_and_notify_schedule_reviews(self.tg, manual=True))
            await asyncio.wait_for(started.wait(), 1)
            self.cb.message.message_id = 100
            await bot.show_user_screen(self.cb, 'Other menu')
            release.set()
            await task
            show.assert_not_awaited()

    async def test_background_and_drive_sync_have_no_recognition_call(self):
        tree = ast.parse(Path(bot.__file__).read_text())
        for name in ('gdrive_sync_background_loop','cb_admin_gdrive_sync'):
            function = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            calls = [ast.unparse(n.func) for n in ast.walk(function) if isinstance(n,ast.Call)]
            self.assertNotIn('prepare_and_notify_schedule_reviews', calls)
            self.assertNotIn('run_gemini_worker', calls)


class GeminiProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_failure_is_reported_and_does_not_leave_busy_flag(self):
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('private'))):
            with self.assertRaisesRegex(review.GeminiError, 'запустить'):
                await bot.run_gemini_worker(99, 'prepare')
        self.assertNotIn(99, bot.gemini_jobs)

    async def test_stubborn_child_is_killed_after_termination_wait(self):
        from unittest.mock import Mock
        process = NS(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError()),
                     terminate=Mock(), kill=Mock(), wait=AsyncMock(side_effect=[asyncio.TimeoutError(), 0]))
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await bot.run_gemini_worker(99, 'prepare')
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)
        self.assertNotIn(99, bot.gemini_jobs)

    async def test_cancellation_terminates_and_reaps_worker_then_allows_retry(self):
        started = asyncio.Event()
        async def communicate(*args):
            started.set()
            await asyncio.Event().wait()
        process = NS(returncode=None, communicate=AsyncMock(side_effect=communicate),
                     terminate=lambda: None, wait=AsyncMock(return_value=0))
        process.terminate = unittest.mock.Mock()
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(bot.run_gemini_worker(99, 'prepare'))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            process.terminate.assert_called_once()
            process.wait.assert_awaited_once()
            self.assertNotIn(99, bot.gemini_jobs)
            process.returncode = 0
            process.communicate = AsyncMock(return_value=(b'{"result":null}', b''))
            self.assertIsNone(await bot.run_gemini_worker(99, 'prepare'))

    async def test_worker_entrypoint_handles_unknown_operation_without_network(self):
        self.assertIsNone(bot.gemini_jobs.get(99))
        with self.assertRaisesRegex(review.GeminiError, 'Неизвестное'):
            await bot.run_gemini_worker(99, 'invalid')
        self.assertNotIn(99, bot.gemini_jobs)
