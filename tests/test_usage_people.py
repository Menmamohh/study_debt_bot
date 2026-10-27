import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database as db
import keyboards as kb


class UsagePeopleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'people.db')
        db.init_db()
        db.add_user_request(10, 'student', 'Telegram', '<Student>', 'ТТ-36')
        db.set_user_status(10, 'approved')
        db.add_user_request(11, None, 'Pending', 'Pending', 'ХК-37')

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_history_filter_identity_and_status(self):
        with db.get_connection() as conn:
            for user, section, date in [(10, 'schedule', 'now'), (10, 'study', 'now'),
                                        (11, 'schedule', 'now'), (10, 'schedule', '-31 days')]:
                conn.execute('INSERT INTO usage_events(user_id, section, created_at) VALUES (?, ?, datetime("now", ?))',
                             (user, section, '+0 days' if date == 'now' else date))
        result = db.get_usage_people(7, 'schedule')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['events'][0]['student_name'], '<Student>')
        self.assertEqual(result['events'][0]['username'], '@student')
        self.assertEqual(db.get_usage_people(30)['total'], 2)
        db.set_user_status(10, 'rejected')
        self.assertEqual(db.get_usage_people()['total'], 0)

    def test_pagination_is_stable_and_clamps_stale_page(self):
        for _ in range(18):
            db.record_usage_event(10, 'profile', 'ТТ-36')
        first = db.get_usage_people(page=0)
        second = db.get_usage_people(page=1)
        self.assertEqual(len(first['events']), 8)
        self.assertFalse({e['id'] for e in first['events']} & {e['id'] for e in second['events']})
        self.assertEqual(db.get_usage_people(page=999)['page'], 2)
        with self.assertRaises(ValueError):
            db.get_usage_people(section="profile' OR 1=1")

    def test_handler_renders_escaped_identity_minsk_and_navigation(self):
        db.record_usage_event(10, 'schedule', 'ТТ-36')
        with db.get_connection() as conn:
            conn.execute("UPDATE usage_events SET created_at=datetime('now', 'start of day')")
        callback = SimpleNamespace(data='adm_stats_people_7_all_0', from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(chat=SimpleNamespace(type='private')), answer=AsyncMock())
        screen = AsyncMock()
        with patch.object(bot, 'is_admin', return_value=True), patch.object(bot, 'show_user_screen', screen):
            asyncio.run(bot.cb_admin_stats_people(callback))
        text = screen.call_args.args[1]
        self.assertIn('&lt;Student&gt;', text)
        self.assertIn('@student', text)
        self.assertIn('03:00:00', text)
        self.assertIn('Расписание', text)
        self.assertIn('Страница 1/1', text)
        for row in screen.call_args.kwargs['reply_markup'].inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_handler_denies_nonadmin_and_group_before_database_read(self):
        for admin, chat in [(False, 'private'), (True, 'group')]:
            callback = SimpleNamespace(data='adm_stats_people_7_all_0', from_user=SimpleNamespace(id=10),
                message=SimpleNamespace(chat=SimpleNamespace(type=chat)), answer=AsyncMock())
            query = AsyncMock()
            with patch.object(bot, 'is_admin', return_value=admin), patch.object(bot.adb, 'get_usage_people', query):
                asyncio.run(bot.cb_admin_stats_people(callback))
            query.assert_not_awaited()
            self.assertTrue(callback.answer.call_args.kwargs['show_alert'])

    def test_empty_results_and_pagination_buttons(self):
        self.assertEqual(db.get_usage_people(page=8)['page'], 0)
        markup = kb.admin_stats_people_kb(30, 'materials', 1, 18)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('adm_stats_people_30_materials_0', callbacks)
        self.assertIn('adm_stats_people_30_materials_2', callbacks)
        self.assertIn('adm_stats_30', callbacks)


if __name__ == '__main__':
    unittest.main()
