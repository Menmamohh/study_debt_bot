import asyncio
import io
import json
import socket
import ssl
import threading
import unittest
import urllib.error
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import keyboards as kb
import schedule_review as review


class GeminiRetryTests(unittest.TestCase):
    def setUp(self):
        self.request = review.urllib.request.Request('https://example.invalid')
        self.sleep = patch.object(review.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def test_transient_server_errors_retry_once_and_recover(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code), patch.object(review.urllib.request, 'urlopen', side_effect=[
                    urllib.error.HTTPError('url', code, 'private', {}, None), io.BytesIO(b'{"ok":true}')]) as send:
                self.assertEqual(review._gemini_json_request(self.request, 'Фото', 90), {'ok': True})
                self.assertEqual(send.call_count, 2)
                self.assertEqual(send.call_args.kwargs['timeout'], 90)

    def test_timeout_and_disconnect_retry_without_leaking_messages(self):
        for failure in (TimeoutError('private'), ConnectionResetError('private'),
                        urllib.error.URLError(socket.gaierror('private'))):
            with self.subTest(failure=type(failure)), patch.object(review.urllib.request, 'urlopen',
                    side_effect=[failure, io.BytesIO(b'{}')]) as send:
                self.assertEqual(review._gemini_json_request(self.request, 'Фото', 90), {})
                self.assertEqual(send.call_count, 2)

    def test_exhausted_retries_are_bounded_and_offer_manual_retry(self):
        with patch.object(review.urllib.request, 'urlopen', side_effect=TimeoutError('private')) as send:
            with self.assertRaises(review.GeminiError) as error:
                review._gemini_json_request(self.request, 'Фото', 90)
        self.assertEqual(send.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.assertIn('повторно запросить', str(error.exception))
        self.assertNotIn('private', str(error.exception))

    def test_permanent_errors_and_certificate_failures_are_not_retried(self):
        failures = [urllib.error.HTTPError('url', code, 'private', {}, None) for code in (400,401,403,404,429)]
        failures += [urllib.error.URLError(ssl.SSLCertVerificationError('private')),
                     ssl.SSLError('private')]
        for failure in failures:
            with self.subTest(failure=type(failure)), patch.object(review.urllib.request, 'urlopen', side_effect=failure) as send:
                with self.assertRaises(review.GeminiError) as error:
                    review._gemini_json_request(self.request, 'Фото', 90)
                self.assertEqual(send.call_count, 1)
                self.assertNotIn('private', str(error.exception))


class ManualDraftRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.schedule_prepare_lock = None
        self.cb = NS(from_user=NS(id=99), message=NS(chat=NS(type='private')), bot=NS(), answer=AsyncMock())
        self.state = NS(clear=AsyncMock())

    def test_button_is_available_in_schedule_section(self):
        self.assertIn('adm_drafts_retry', {b.callback_data for row in kb.admin_section_kb('schedule').inline_keyboard for b in row})

    async def test_admin_and_private_chat_required(self):
        for allowed, chat in ((False,'private'), (True,'group')):
            self.cb.message.chat.type = chat
            with patch.object(bot, 'is_admin', return_value=allowed), patch.object(bot, 'prepare_and_notify_schedule_reviews', AsyncMock()) as prepare:
                await bot.cb_admin_drafts_retry(self.cb, self.state)
                prepare.assert_not_awaited()
        self.state.clear.assert_not_awaited()

    async def test_manual_request_opens_existing_drafts_without_drive_sync(self):
        with patch.object(bot, 'is_admin', return_value=True), patch.object(bot, 'ADMIN_ID', 99), \
                patch.object(bot, 'update_review_screen', AsyncMock()), \
                patch.object(bot, 'show_next_schedule_review', AsyncMock()) as show, \
                patch.object(review, 'prepare_reviews') as prepare, \
                patch.object(review, 'pending_reviews', return_value=[]), \
                patch.object(bot.gdrive_sync, 'sync_drive_schedule') as sync:
            await bot.cb_admin_drafts_retry(self.cb, self.state)
            prepare.assert_called_once()
            show.assert_awaited_once_with(self.cb.bot, 99)
            sync.assert_not_called()
            self.state.clear.assert_awaited_once()

    async def test_double_click_does_not_queue_another_request(self):
        started, release = threading.Event(), threading.Event()
        def prepare():
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker not released')
        with patch.object(bot, 'is_admin', return_value=True), patch.object(bot, 'ADMIN_ID', 99), \
                patch.object(bot, 'update_review_screen', AsyncMock()), \
                patch.object(bot, 'show_next_schedule_review', AsyncMock()), \
                patch.object(review, 'prepare_reviews', side_effect=prepare) as calls, \
                patch.object(review, 'pending_reviews', return_value=[]):
            first = asyncio.create_task(bot.cb_admin_drafts_retry(self.cb, self.state))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                await bot.cb_admin_drafts_retry(self.cb, self.state)
                self.assertEqual(calls.call_count, 1)
                self.assertIn('уже запрашиваются', self.cb.answer.await_args.args[0])
            finally:
                release.set()
                await first
        self.assertFalse(bot.schedule_prepare_lock.locked())

    async def test_failure_keeps_retry_button_and_releases_busy_flag(self):
        with patch.object(bot, 'ADMIN_ID', 99), patch.object(review, 'prepare_reviews', side_effect=review.GeminiError('HTTP 503')), \
                patch.object(bot, 'update_review_screen', AsyncMock()) as screen:
            self.assertFalse(await bot.prepare_and_notify_schedule_reviews(self.cb.bot, manual=True))
            markup = screen.await_args.args[3]
            self.assertIn('adm_drafts_retry', {b.callback_data for row in markup.inline_keyboard for b in row})
        self.assertFalse(bot.schedule_prepare_lock.locked())
