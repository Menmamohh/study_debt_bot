"""Study navigation against a temporary SQLite database and mocked Telegram only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

import bot
import database as db
import keyboards as kb


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


class StudyNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'study.db')
        db.init_db()
        self.user = NS(id=10, username='student', full_name='Student')
        db.add_user_request(10, 'student', 'Student', 'Student', 'ТТ-36')
        db.set_user_status(10, 'approved')
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=10, user_id=10))
        self.telegram = NS(send_message=AsyncMock(return_value=NS(message_id=200)),
                           send_photo=AsyncMock(return_value=NS(message_id=201)),
                           send_document=AsyncMock(return_value=NS(message_id=202)),
                           delete_message=AsyncMock())
        self.screen_patch = patch.object(bot, 'show_user_screen', AsyncMock())
        self.screen = self.screen_patch.start()
        self.admin_patch = patch.object(bot, 'ADMIN_ID', 99)
        self.admin_patch.start()

    def tearDown(self):
        self.admin_patch.stop()
        self.screen_patch.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def callback(self, data, photo=False, message_id=100):
        message = NS(chat=NS(id=10, type='private'), message_id=message_id,
                     photo=[NS(file_id='menu')] if photo else None, bot=self.telegram,
                     edit_text=AsyncMock(), edit_caption=AsyncMock(), delete=AsyncMock(),
                     answer=AsyncMock(), answer_photo=AsyncMock(), answer_document=AsyncMock())
        return NS(data=data, from_user=self.user, message=message,
                  bot=self.telegram, answer=AsyncMock())

    def message(self, text=None, photo=False):
        return NS(text=text, caption='Мой материал' if photo else None,
                  photo=[NS(file_id='student-photo')] if photo else None,
                  document=None, from_user=self.user, chat=NS(type='private', id=10),
                  bot=self.telegram)

    def rendered(self, cb):
        if cb.message.edit_caption.await_count:
            call = cb.message.edit_caption.call_args
            return call.kwargs['caption'], call.kwargs['reply_markup']
        call = cb.message.edit_text.call_args
        return call.args[0], call.kwargs['reply_markup']

    def assert_nav(self, markup, back):
        callbacks = {b.callback_data for b in buttons(markup)}
        self.assertIn(back, callbacks)
        self.assertIn('main_menu', callbacks)
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in buttons(markup)))

    async def test_empty_tracker_three_buttons_and_direct_subjects(self):
        cb = self.callback('debts_hub', photo=True)
        await bot.cb_debts_hub(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('📝 <b>Моя учёба</b>', text)
        self.assertIn('Осталось сдать: 0 · Сдано: 0 из 0', text)
        self.assertIn('Работ пока нет. Открой предмет, чтобы добавить первую', text)
        self.assertNotIn('Все добавленные работы сданы', text)
        self.assertEqual([b.text for b in buttons(markup)],
                         ['📚 Мои работы', '📖 Методички и решения', '🏠 Главное меню'])
        self.assertEqual([b.callback_data for b in buttons(markup)],
                         ['list_subjects', 'browse_materials_hub', 'main_menu'])
        cb.message.edit_text.assert_not_awaited()
        db.seed_default_subjects(10)
        cb = self.callback('list_subjects')
        await bot.cb_list_subjects(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('Мои работы', text)
        self.assertIn('Работ пока нет.', text)
        self.assert_nav(markup, 'debts_hub')
        callbacks = [b.callback_data for b in buttons(markup)]
        self.assertIn('show_all_debts', callbacks)
        self.assertIn('add_subject_prompt', callbacks)
        # The first level is already the subject list, with no action chooser.
        subject = db.get_subjects_with_counts(10)[0]
        self.assertIn(f"open_sub_{subject['id']}", callbacks)
        self.assertTrue(any(b.text.startswith('⚪') for b in buttons(markup)))

    async def test_pending_done_counts_and_toggle(self):
        subject = db.add_subject(10, '<Математика>')
        task = db.add_task(10, subject, 'Работа 1')
        db.add_task(10, subject, 'Работа 2')
        cb = self.callback('debts_hub')
        await bot.cb_debts_hub(cb, self.state)
        self.assertIn('Осталось сдать: 2 · Сдано: 0 из 2', self.rendered(cb)[0])
        cb = self.callback('list_subjects')
        await bot.cb_list_subjects(cb, self.state)
        self.assertTrue(any('не сдано: 2' in b.text for b in buttons(self.rendered(cb)[1])))
        cb = self.callback(f'open_sub_{subject}', photo=True)
        await bot.cb_open_subject(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('&lt;Математика&gt;', text)
        self.assertIn('0/2 сдано', text)
        self.assert_nav(markup, 'list_subjects')
        self.assertIn(f'add_task_prompt_{subject}', [b.callback_data for b in buttons(markup)])
        cb = self.callback(f'toggle_t_{task}_{subject}')
        await bot.cb_toggle_task(cb)
        self.assertIn('1/2 сдано', self.rendered(cb)[0])
        other = next(t['id'] for t in db.get_tasks_for_subject(10, subject) if t['id'] != task)
        await bot.cb_toggle_task(self.callback(f'toggle_t_{other}_{subject}'))
        cb = self.callback('debts_hub')
        await bot.cb_debts_hub(cb, self.state)
        text, _ = self.rendered(cb)
        self.assertIn('Осталось сдать: 0 · Сдано: 2 из 2', text)
        self.assertIn('Все добавленные работы сданы', text)
        self.assertNotIn('Работ пока нет.', text)
        cb = self.callback('show_all_debts')
        await bot.cb_show_all_debts(cb, self.state)
        self.assertIn('Все добавленные работы сданы', self.rendered(cb)[0])
        self.assert_nav(self.rendered(cb)[1], 'list_subjects')
        self.assertEqual(db.get_user(10)['status'], 'approved')
        self.assertEqual(db.get_user(10)['student_group'], 'ТТ-36')

    async def test_add_subject_and_work_and_back_clear_input(self):
        cb = self.callback('add_subject_prompt', photo=True)
        await bot.cb_add_subject_prompt(cb, self.state)
        self.assertEqual(await self.state.get_state(), bot.Form.waiting_for_subject_name.state)
        self.assert_nav(self.rendered(cb)[1], 'list_subjects')
        await bot.process_subject_name(self.message('Новый предмет'), self.state)
        subject = next(s['id'] for s in db.get_subjects_with_counts(10) if s['name']=='Новый предмет')
        self.assertIsNone(await self.state.get_state())
        cb = self.callback(f'add_task_prompt_{subject}', photo=True)
        await bot.cb_add_task_prompt(cb, self.state)
        self.assert_nav(self.rendered(cb)[1], f'open_sub_{subject}')
        await bot.process_task_title(self.message('Работа A\nРабота Б'), self.state)
        self.assertEqual(len(db.get_tasks_for_subject(10, subject)), 2)
        await bot.cb_add_task_prompt(self.callback(f'add_task_prompt_{subject}'), self.state)
        await bot.cb_open_subject(self.callback(f'open_sub_{subject}'), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_materials_browse_upload_moderation_and_legacy_buttons(self):
        subject = db.add_subject(10, 'Материаловедение')
        task = db.add_task(10, subject, 'Работа')
        approved = db.add_material(10, '@student', task, 'method', 'Проверено', content_text='Текст')
        pending = db.add_material(10, '@student', task, 'solution', 'Скрыто', content_text='Черновик')
        db.set_material_status(approved, 'approved')
        cb = self.callback('browse_materials_hub', photo=True)
        await bot.cb_browse_materials_hub(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('Методички и решения', text)
        self.assert_nav(markup, 'debts_hub')
        self.assertIn('📤 Отправить свой материал', [b.text for b in buttons(markup)])
        token = kb.subject_callback_token('Материаловедение')
        cb = self.callback(f'b_sub_{token}', photo=True)
        await bot.cb_browse_subject_materials(cb, self.state)
        callbacks = [b.callback_data for b in buttons(self.rendered(cb)[1])]
        self.assertIn(f'dl_mat_{approved}', callbacks)
        self.assertNotIn(f'dl_mat_{pending}', callbacks)
        await bot.cb_download_material(self.callback(f'dl_mat_{approved}'))
        cb = self.callback(f'view_mat_{task}_{subject}')
        await bot.cb_view_materials(cb, self.state)
        self.assert_nav(self.rendered(cb)[1], f'open_sub_{subject}_0')
        cb = self.callback('quick_upload_material', photo=True)
        await bot.cb_quick_upload_material(cb, self.state)
        self.assert_nav(self.rendered(cb)[1], 'browse_materials_hub')
        # Real aiogram callbacks are frozen: the old subject-name button must still work.
        real = CallbackQuery(id='x', chat_instance='x', from_user=User(id=10, is_bot=False, first_name='Student'),
                             data=f'quick_sub_name_{token}',
                             message=Message(message_id=100, date=0, chat=Chat(id=10, type='private')))
        with patch.object(bot, 'safe_edit_message', AsyncMock()) as render:
            await bot.cb_quick_sub_by_name(real, self.state)
            self.assertIn(f'quick_task_{task}_{subject}',
                          [b.callback_data for b in buttons(render.call_args.kwargs['reply_markup'])])
        cb = self.callback(f'quick_task_{task}_{subject}', photo=True)
        await bot.cb_quick_task_select(cb)
        self.assert_nav(self.rendered(cb)[1], f'view_mat_{task}_{subject}')
        # The previous per-work upload button is also retained.
        cb = self.callback(f'upload_mat_{task}_{subject}', photo=True)
        await bot.cb_upload_material(cb)
        self.assert_nav(self.rendered(cb)[1], f'view_mat_{task}_{subject}')
        cb = self.callback(f'set_mat_type_solution_{task}_{subject}', photo=True)
        await bot.cb_set_material_type(cb, self.state, self.telegram)
        self.assert_nav(self.rendered(cb)[1], f'view_mat_{task}_{subject}')
        await bot.process_material_file(self.message(photo=True), self.state, self.telegram)
        with db.get_connection() as conn:
            created = dict(conn.execute('SELECT * FROM materials ORDER BY id DESC LIMIT 1').fetchone())
        self.assertEqual(created['status'], 'pending')
        self.assertEqual(created['file_id'], 'student-photo')
        self.assertEqual(created['task_id'], task)
        self.telegram.send_photo.assert_awaited_once()
        self.assertEqual(self.telegram.send_photo.call_args.kwargs['chat_id'], 99)
        self.assertIsNone(await self.state.get_state())
        self.assertIn('передан на верификацию', self.screen.call_args.args[1])

    async def test_navigation_replaces_stale_photo_and_returns_home(self):
        db.set_ui_message_id(10, 90)
        cb = self.callback('debts_hub', photo=True)
        await bot.cb_debts_hub(cb, self.state)
        self.telegram.send_message.assert_awaited_once()
        cb.message.edit_caption.assert_not_awaited()
        removed = [c.kwargs['message_id'] for c in self.telegram.delete_message.call_args_list]
        self.assertIn(90, removed)
        self.assertIn(100, removed)
        await self.state.set_state(bot.Form.waiting_for_subject_name)
        await self.state.update_data(current_subject_id=123)
        await bot.cb_main_menu(self.callback('main_menu'), self.state)
        self.assertIn('ПОМОЩНИК ПЯТНАШКИ', self.screen.call_args.args[1])
        self.assertIn('Мои работы', self.screen.call_args.args[1])
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_group_access_and_foreign_subject_remain_restricted(self):
        cb = self.callback('debts_hub')
        db.add_user_request(20, 'other', 'Other', 'Other', 'С-37')
        db.set_user_status(20, 'approved')
        cb.from_user = NS(id=20)
        with patch.object(bot, 'reject_non_tt36', AsyncMock()) as reject:
            await bot.cb_debts_hub(cb, self.state)
            reject.assert_awaited_once()
        foreign = db.add_subject(20, 'Чужой предмет')
        cb = self.callback(f'open_sub_{foreign}')
        await bot.cb_open_subject(cb, self.state)
        cb.message.edit_text.assert_not_awaited()
        self.assertTrue(cb.answer.call_args.kwargs['show_alert'])


    async def test_empty_subject_all_debts_and_unsent_input(self):
        subject = db.add_subject(10, 'Пустой предмет')
        for data, handler in [('show_all_debts', bot.cb_show_all_debts),
                              (f'open_sub_{subject}', bot.cb_open_subject)]:
            cb = self.callback(data, photo=True)
            await handler(cb, self.state)
            self.assertIn('Работ пока нет. Открой предмет, чтобы добавить первую', self.rendered(cb)[0])
            self.assertNotIn('Все добавленные работы сданы', self.rendered(cb)[0])
        cb = self.callback(f'quick_sub_{subject}', photo=True)
        await bot.cb_quick_sub_select(cb, self.state)
        self.assertEqual(await self.state.get_state(), bot.Form.waiting_for_quick_task_title.state)
        self.assert_nav(self.rendered(cb)[1], 'quick_upload_material')
        await bot.process_quick_task_title(self.message('Новая работа'), self.state, self.telegram)
        task = db.get_tasks_for_subject(10, subject)[0]['id']
        cb = self.callback(f'set_mat_type_method_{task}_{subject}')
        await bot.cb_set_material_type(cb, self.state, self.telegram)
        # Back cancels the input; the following message must not become a material.
        await bot.cb_view_materials(self.callback(f'view_mat_{task}_{subject}'), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual(db.count_pending_materials(), 0)

    async def test_text_material_and_pending_debts(self):
        subject = db.add_subject(10, 'Предмет')
        task = db.add_task(10, subject, 'Работа')
        cb = self.callback('show_all_debts')
        await bot.cb_show_all_debts(cb, self.state)
        self.assertIn('Работа', self.rendered(cb)[0])
        self.assertIn('Осталось сдать:', self.rendered(cb)[0])
        await bot.cb_set_material_type(self.callback(f'set_mat_type_solution_{task}_{subject}'), self.state, self.telegram)
        await bot.process_material_file(self.message('Решение <пример>'), self.state, self.telegram)
        self.assert_nav(self.screen.call_args.kwargs['reply_markup'], f'view_mat_{task}_{subject}')
        await bot.cb_save_text_mat(self.callback(f'save_text_mat_{task}_{subject}'), self.state, self.telegram)
        self.assertEqual(db.count_pending_materials(), 1)
        self.assertEqual(len(db.get_approved_materials(task)), 0)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(self.telegram.send_message.call_args.kwargs['chat_id'], 99)
        self.assertIsNone(await self.state.get_state())

    async def test_all_debts_pagination_and_short_callback(self):
        subject = db.add_subject(10, 'Предмет')
        for index in range(12):
            db.add_task(10, subject, f'Работа {index:02d}')
        cb = self.callback('show_all_debts')
        await bot.cb_show_all_debts(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('Работа 00', text)
        self.assertNotIn('Работа 10', text)
        self.assertIn('show_all_debts_1', [b.callback_data for b in buttons(markup)])
        cb = self.callback('show_all_debts_1', photo=True)
        await bot.cb_show_all_debts(cb, self.state)
        text, markup = self.rendered(cb)
        self.assertIn('Работа 10', text)
        self.assertIn('Страница 2 из 2', text)
        self.assertNotIn('Работа 00', text)
        self.assert_nav(markup, 'list_subjects')

    async def test_legacy_callbacks_select_correct_handlers(self):
        expected = {
            'debts_hub': bot.cb_debts_hub,
            'list_subjects': bot.cb_list_subjects,
            'show_all_debts': bot.cb_show_all_debts,
            'show_all_debts_1': bot.cb_show_all_debts,
            'add_subject_prompt': bot.cb_add_subject_prompt,
            'browse_materials_hub': bot.cb_browse_materials_hub,
            'quick_upload_material': bot.cb_quick_upload_material,
            'open_sub_1': bot.cb_open_subject,
            'quick_sub_name_abcdef': bot.cb_quick_sub_by_name,
            'quick_sub_1': bot.cb_quick_sub_select,
            'quick_new_task_1': bot.cb_quick_new_task,
            'quick_task_1_1': bot.cb_quick_task_select,
            'upload_mat_1_1': bot.cb_upload_material,
        }
        for data, target in expected.items():
            event = CallbackQuery(id='x', chat_instance='x', data=data,
                                  from_user=User(id=10, is_bot=False, first_name='Student'),
                                  message=Message(message_id=100, date=0, chat=Chat(id=10, type='private')))
            for handler in bot.dp.callback_query.handlers:
                matched, _ = await handler.check(event)
                if matched:
                    self.assertIs(handler.callback, target, data)
                    break
            else:
                self.fail(f'No callback handler for {data}')


if __name__ == '__main__':
    unittest.main()
