import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import bot
import keyboards as kb


def callbacks(markup):
    return {button.callback_data for row in markup.inline_keyboard for button in row}


class AdminNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=99, user_id=99))
        self.cb = NS(data="adm_section_students", from_user=NS(id=99),
                     message=NS(chat=NS(type="private")), answer=AsyncMock())
        bot.admin_removal_lock = None

    def test_every_previous_action_has_a_section_and_root_return(self):
        actions = set()
        for section in kb.ADMIN_SECTIONS:
            menu = callbacks(kb.admin_section_kb(section))
            self.assertIn("admin_panel", menu)
            actions |= menu
        self.assertTrue({"adm_users_queue", "adm_users_list", "adm_gdrive_sync", "sr_queue",
                         "adm_schedule_groups", "adm_sched_publish_all", "adm_broadcast_start",
                         "admin_review_queue", "adm_manage_materials", "adm_feedback_queue",
                         "adm_test_mode", "adm_ocr_mode"} <= actions)
        empty = callbacks(kb.admin_panel_kb())
        busy = callbacks(kb.admin_panel_kb(12, 34, 56))
        self.assertEqual(empty, busy)
        self.assertTrue({f"adm_section_{section}" for section in kb.ADMIN_SECTIONS} <= empty)

    async def test_sections_cancel_form_without_sending_or_publishing(self):
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot, "show_user_screen", AsyncMock()) as screen:
            for section in kb.ADMIN_SECTIONS:
                await self.state.set_state(bot.Form.waiting_for_broadcast)
                await self.state.update_data(announcement_message_id=12)
                self.cb.data = f"adm_section_{section}"
                await bot.cb_admin_section(self.cb, self.state)
                self.assertIsNone(await self.state.get_state())
                self.assertEqual(await self.state.get_data(), {})
                self.assertIn(kb.ADMIN_SECTIONS[section], screen.call_args.args[1])

    async def test_private_admin_only_and_unknown_section(self):
        with patch.object(bot, "is_admin", return_value=False), \
             patch.object(bot, "show_user_screen", AsyncMock()) as screen:
            await bot.cb_admin_section(self.cb, self.state)
            screen.assert_not_awaited()
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot, "show_user_screen", AsyncMock()) as screen:
            self.cb.message.chat.type = "group"
            await bot.cb_admin_section(self.cb, self.state)
            self.cb.message.chat.type = "private"
            self.cb.data = "adm_section_obsolete"
            await bot.cb_admin_section(self.cb, self.state)
            screen.assert_not_awaited()

    async def test_schedule_groups_reach_existing_views_with_short_callbacks(self):
        self.cb.data = "adm_schedule_groups"
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot, "show_user_screen", AsyncMock()) as screen:
            await bot.cb_admin_section(self.cb, self.state)
        actions = callbacks(screen.call_args.kwargs['reply_markup'])
        self.assertIn("sched_view_ALL", actions)
        self.assertTrue({f"sched_view_{group}" for group in bot.db.COLLEGE_GROUPS} <= actions)
        for action in actions:
            self.assertLessEqual(len(action.encode()), 64)

    async def test_middleware_blocks_legacy_admin_buttons_and_cancels_input(self):
        event = CallbackQuery(id="1", chat_instance="1", data="adm_users_queue",
                              from_user={"id": 99, "is_bot": False, "first_name": "Admin"},
                              message={"message_id": 1, "date": 0, "chat": {"id": 99, "type": "private"}})
        handler = AsyncMock()
        with patch.object(CallbackQuery, 'answer', AsyncMock()), \
             patch.object(bot, "is_admin", return_value=False):
            await bot.verification_middleware(handler, event, {"state": self.state})
            handler.assert_not_awaited()
        await self.state.set_state(bot.Form.waiting_for_broadcast)
        await self.state.update_data(announcement_message_id=1)
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "cancel_feedback_reply_drafts", AsyncMock()):
            await bot.verification_middleware(handler, event, {"state": self.state})
        handler.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_clear_requires_fresh_confirmation_and_consumes_it_once(self):
        self.cb.data = "adm_sched_clear_ТТ-36"
        snapshot = {"schedule_text": "original"}
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "get_group_schedule", AsyncMock(return_value=snapshot)), \
             patch.object(bot.adb, "clear_group_schedule", AsyncMock()) as clear, \
             patch.object(bot, "show_user_screen", AsyncMock()):
            await bot.cb_admin_sched_clear(self.cb, self.state)
            clear.assert_not_awaited()
        token = (await self.state.get_data())['admin_removal']['token']
        self.cb.data = f"adm_remove_{token}"
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "get_group_schedule", AsyncMock(return_value=snapshot)), \
             patch.object(bot.adb, "clear_group_schedule", AsyncMock()) as clear, \
             patch.object(bot, "render_group_schedule_view", AsyncMock()):
            await asyncio.gather(bot.cb_admin_schedule_removal(self.cb, self.state),
                                 bot.cb_admin_schedule_removal(self.cb, self.state))
            clear.assert_awaited_once_with("ТТ-36")

    async def test_changed_schedule_or_cancelled_form_cannot_be_cleared(self):
        with patch.object(bot, "show_user_screen", AsyncMock()):
            await bot.confirm_schedule_removal(self.cb, self.state, "ТТ-36", {"body": "old"})
        token = (await self.state.get_data())['admin_removal']['token']
        self.cb.data = f"adm_remove_{token}"
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "get_group_schedule", AsyncMock(return_value={"body": "new"})), \
             patch.object(bot.adb, "clear_group_schedule", AsyncMock()) as clear:
            await bot.cb_admin_schedule_removal(self.cb, self.state)
            clear.assert_not_awaited()
            await self.state.clear()
            await bot.cb_admin_schedule_removal(self.cb, self.state)
            clear.assert_not_awaited()

    async def test_old_version_requires_confirmation_and_passes_exact_id(self):
        old = {'id': 12, 'group_name': 'ТТ-36', 'body': 'old'}
        versions = [{'id': 13, 'group_name': 'ТТ-36'}, old]
        self.cb.data = "schdel_12_ТТ-36"
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot, "check_user_access", AsyncMock(return_value=(True, None))), \
             patch.object(bot.adb, "get_schedule_versions", AsyncMock(return_value=versions)), \
             patch.object(bot.adb, "delete_old_schedule_version", AsyncMock(return_value=True)) as delete, \
             patch.object(bot, "render_group_schedule_view", AsyncMock()), \
             patch.object(bot, "show_user_screen", AsyncMock()):
            await bot.cb_schedule_history(self.cb, self.state)
            delete.assert_not_awaited()
            token = (await self.state.get_data())['admin_removal']['token']
            self.cb.data = f"adm_remove_{token}"
            await bot.cb_admin_schedule_removal(self.cb, self.state)
            delete.assert_awaited_once_with(12)

    async def test_unknown_admin_button_is_acknowledged_without_action(self):
        self.cb.data = "adm_obsolete_action"
        with patch.object(bot, "is_admin", return_value=True):
            await bot.cb_stale_admin_button(self.cb)
        self.assertIn("устарела", self.cb.answer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
