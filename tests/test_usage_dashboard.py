import asyncio
import html
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database as db
import usage_stats_ui as ui
from aiogram.types import CallbackQuery, User, Message, Chat


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'dashboard.db')
        db.init_db()
        for uid in range(10, 22):
            db.add_user_request(uid, 'user' + str(uid), 'Telegram', '<Student & ' + str(uid) + '>', 'ТТ-36')
            db.set_user_status(uid, 'approved' if uid < 21 else 'pending')

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_today_starts_at_minsk_midnight_even_before_utc_midnight(self):
        with patch.object(db, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 28, 22, 30, tzinfo=timezone.utc)
            self.assertEqual(db._usage_period(0), (0, '2026-10-28 21:00:00'))
            self.assertEqual(db._usage_period(7), (7, '2026-10-21 22:30:00'))

    def test_all_today_views_use_same_boundary(self):
        with db.get_connection() as conn:
            for uid, date in [(10, '2026-10-28 20:59:59'), (11, '2026-10-28 21:00:00'),
                              (11, '2026-10-28 21:05:00'), (21, '2026-10-28 22:00:00')]:
                conn.execute('INSERT INTO usage_events(user_id, section, created_at) VALUES (?, ?, ?)', (uid, 'schedule', date))
        with patch.object(db, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 28, 22, 30, tzinfo=timezone.utc)
            self.assertEqual(db.get_usage_stats(0)['active'], 1)
            self.assertEqual(db.get_usage_stats(0)['sections'][0]['opens'], 2)
            self.assertEqual(db.get_usage_people(0)['total'], 2)
            self.assertEqual(db.get_usage_users(0)['total'], 1)

    def test_people_are_unique_and_pagination_is_stable(self):
        for uid in range(10, 21):
            db.record_usage_event(uid, 'schedule', 'ТТ-36')
        for _ in range(4):
            db.record_usage_event(12, 'useful', 'ТТ-36')
        first, second = db.get_usage_users(), db.get_usage_users(page=1)
        self.assertEqual(first['total'], 11)
        self.assertEqual(first['users'][0]['user_id'], 12)
        self.assertEqual(first['users'][0]['opens'], 5)
        self.assertEqual(len(first['users']), 8)
        self.assertEqual(len(second['users']), 3)
        self.assertFalse({u['user_id'] for u in first['users']} & {u['user_id'] for u in second['users']})
        self.assertEqual(db.get_usage_users(page=999)['page'], 1)

    def test_person_history_never_includes_other_students(self):
        for uid in (10, 11):
            db.record_usage_event(uid, 'schedule', 'ТТ-36')
        result = db.get_usage_people(user_id=11)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['events'][0]['user_id'], 11)
        db.set_user_status(11, 'rejected')
        self.assertEqual(db.get_usage_people(user_id=11)['total'], 0)

    def test_people_screen_links_to_person_and_preserves_back_page(self):
        db.record_usage_event(10, 'profile', 'ТТ-36')
        text, markup = ui.users_screen(db.get_usage_users(), 7)
        self.assertIn('&lt;Student &amp; 10&gt;', text)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('adm_stats_user_7_10_0_0', callbacks)
        result = db.get_usage_people(user_id=10)
        text, markup = ui.events_screen(result, 7, user_id=10, list_page=3)
        self.assertIn('Всего открытий', text)
        self.assertIn('adm_stats_users_7_3', [b.callback_data for row in markup.inline_keyboard for b in row])

    def test_empty_screens_have_clear_messages_and_return_buttons(self):
        views = [ui.overview(db.get_usage_stats(0), 0), ui.groups_screen(db.get_usage_stats(0), 0),
                 ui.users_screen(db.get_usage_users(0), 0), ui.events_screen(db.get_usage_people(0), 0),
                 ui.events_screen(db.get_usage_people(0, user_id=10), 0, user_id=10)]
        for text, markup in views:
            self.assertIn('Сегодня', text)
            self.assertNotIn('None', text)
            self.assertIn('admin_panel', [b.callback_data for row in markup.inline_keyboard for b in row])
        self.assertIn('администратора не учитываются', views[0][0])

    def test_large_group_list_is_paginated(self):
        stats = {'active': 30, 'groups': [{'group_name': '<Group '+str(i)+'>', 'active': 2} for i in range(30)]}
        text, markup = ui.groups_screen(stats, 30, 2)
        self.assertIn('&lt;Group 16&gt;', text)
        self.assertNotIn('&lt;Group 24&gt;', text)
        self.assertIn('adm_stats_groups_30_1', [b.callback_data for row in markup.inline_keyboard for b in row])
        self.assertIn('adm_stats_groups_30_3', [b.callback_data for row in markup.inline_keyboard for b in row])

    def test_all_new_handlers_deny_students_and_group_chats_before_query(self):
        cases = [(bot.cb_admin_stats, 'adm_stats_0'), (bot.cb_admin_stats_users, 'adm_stats_users_7_0'),
                 (bot.cb_admin_stats_user, 'adm_stats_user_7_10_0_0'), (bot.cb_admin_stats_people, 'adm_stats_people_7_all_0')]
        for handler, action in cases:
            for admin, chat in [(False, 'private'), (True, 'group')]:
                callback = SimpleNamespace(data=action, from_user=SimpleNamespace(id=10),
                    message=SimpleNamespace(chat=SimpleNamespace(type=chat)), answer=AsyncMock())
                query = AsyncMock()
                with patch.object(bot, 'is_admin', return_value=admin), patch.object(bot.adb, 'get_usage_stats', query), \
                     patch.object(bot.adb, 'get_usage_users', query), patch.object(bot.adb, 'get_usage_people', query):
                    asyncio.run(handler(callback))
                query.assert_not_awaited()
                self.assertTrue(callback.answer.call_args.kwargs['show_alert'])

    def test_admin_can_render_all_views_with_real_database(self):
        db.record_usage_event(10, 'schedule', 'ТТ-36')
        for handler, action in [(bot.cb_admin_stats, 'adm_stats_0'), (bot.cb_admin_stats, 'adm_stats_groups_7_0'),
                                (bot.cb_admin_stats_users, 'adm_stats_users_7_0'), (bot.cb_admin_stats_user, 'adm_stats_user_7_10_0_0')]:
            callback = SimpleNamespace(data=action, from_user=SimpleNamespace(id=99),
                message=SimpleNamespace(chat=SimpleNamespace(type='private')), answer=AsyncMock())
            screen = AsyncMock()
            with patch.object(bot, 'is_admin', return_value=True), patch.object(bot, 'show_user_screen', screen):
                asyncio.run(handler(callback))
            screen.assert_awaited_once()
            self.assertIn('reply_markup', screen.call_args.kwargs)

    def test_every_generated_callback_reaches_a_statistics_handler(self):
        db.record_usage_event(10, 'schedule', 'ТТ-36')
        views = []
        for days in (0, 7, 30):
            views += [ui.overview(db.get_usage_stats(days), days), ui.groups_screen(db.get_usage_stats(days), days),
                      ui.users_screen(db.get_usage_users(days), days), ui.events_screen(db.get_usage_people(days), days),
                      ui.events_screen(db.get_usage_people(days), days, user_id=10, list_page=3)]
        callbacks = {b.callback_data for _, markup in views for row in markup.inline_keyboard for b in row}
        async def route():
            for action in callbacks:
                self.assertLessEqual(len(action.encode()), 64)
                if action == 'admin_panel':
                    continue
                event = CallbackQuery(id='test', from_user=User(id=99, is_bot=False, first_name='Admin'),
                    chat_instance='test', data=action,
                    message=Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=99, type='private')))
                for handler in bot.dp.callback_query.handlers:
                    matched, _ = await handler.check(event)
                    if matched:
                        self.assertIn(handler.callback.__name__, {'cb_admin_stats','cb_admin_stats_users','cb_admin_stats_user','cb_admin_stats_people'})
                        break
                else:
                    self.fail('No handler for '+action)
        asyncio.run(route())

    def test_runtime_imports_using_only_docker_copied_python_files(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            for line in (root / 'Dockerfile').read_text().splitlines():
                if line.startswith('COPY ') and line.endswith(' ./'):
                    for source in line.split()[1:-1]:
                        if source.endswith('.py'):
                            shutil.copy2(root / source, runtime / source)
            env = dict(os.environ, PYTHONPATH=str(runtime), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', 'import bot; import usage_stats_ui'],
                                    cwd=runtime, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mobile_text_stays_within_telegram_limit(self):
        for uid in range(10, 21):
            db.record_usage_event(uid, 'schedule', 'ТТ-36')
        with db.get_connection() as conn:
            conn.execute('UPDATE users SET student_name=?, username=?, student_group=?', ('😀&<'*80, '@'+'x'*100, '&'*100))
        for text, _ in [ui.users_screen(db.get_usage_users(), 7), ui.events_screen(db.get_usage_people(), 7)]:
            plain = html.unescape(re.sub('<[^>]*>', '', text))
            self.assertLess(len(plain.encode('utf-16-le'))//2, 4096)


if __name__ == '__main__':
    unittest.main()
