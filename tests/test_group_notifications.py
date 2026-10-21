import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

import bot
import database as db
import keyboards as kb


class GroupNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path, db.DB_PATH = db.DB_PATH, str(Path(self.tmp.name) / "notice.db")
        db.init_db()
        self.admin = User(id=99, is_bot=False, first_name="Admin")
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=99, user_id=99))
        self.callback = NS(data="adm_notice_send", from_user=self.admin,
                           message=NS(chat=NS(type="private")), answer=AsyncMock())
        bot.group_notice_publish_lock = None
        for uid, group, status in [(10, "ТТ-36", "approved"), (11, "ТТ-36", "approved"),
                                   (12, "ХК-37", "approved"), (13, "ТТ-36", "pending")]:
            db.add_user_request(uid, None, "student", "Student", group)
            db.set_user_status(uid, status)

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_notification_menu_offers_group_and_global_flows(self):
        data = {b.callback_data for row in kb.admin_section_kb("notifications").inline_keyboard for b in row}
        self.assertIn("adm_group_notice_start", data)
        self.assertIn("adm_broadcast_start", data)
        self.assertIn("adm_notice_group_ТТ-36", {b.callback_data for row in kb.admin_notice_groups_kb(db.COLLEGE_GROUPS).inline_keyboard for b in row})

    async def test_group_selection_rejects_other_and_empty_audiences(self):
        with patch.object(bot, "is_admin", return_value=True), patch.object(bot, "show_user_screen", AsyncMock()):
            await self.state.update_data(group_notice_token="valid")
            self.callback.data = "adm_notice_group_INVALID"
            await bot.cb_admin_group_notice_select(self.callback, self.state)
            self.assertNotIn("group_notice_group", await self.state.get_data())
            self.callback.data = "adm_notice_group_С-37"
            await bot.cb_admin_group_notice_select(self.callback, self.state)
            self.assertNotIn("group_notice_group", await self.state.get_data())
            self.callback.data = "adm_notice_group_ТТ-36"
            await bot.cb_admin_group_notice_select(self.callback, self.state)
            self.assertEqual((await self.state.get_data())["group_notice_recipients"], [10, 11])

    async def test_text_preview_is_escaped_and_reports_confirmed_group_count(self):
        await self.state.update_data(group_notice_token="valid", group_notice_group="ТТ-36")
        msg = NS(from_user=self.admin, chat=NS(type="private"), text="<hello>")
        with patch.object(bot, "is_admin", return_value=True), patch.object(bot, "show_user_screen", AsyncMock()) as screen:
            await bot.process_group_notice_text(msg, self.state)
        rendered = screen.call_args.args[1]
        self.assertIn("ТТ-36", rendered)
        self.assertIn("Получателей: <b>2</b>", rendered)
        self.assertIn("&lt;hello&gt;", rendered)
        self.assertIn("adm_notice_send", {b.callback_data for row in screen.call_args.kwargs['reply_markup'].inline_keyboard for b in row})

    async def test_delivery_stays_in_selected_group_and_continues_after_failure(self):
        await self.state.update_data(group_notice_token="one-shot", group_notice_group="ТТ-36", group_notice_body="Update")
        callback = self.callback
        def send(**kwargs):
            if kwargs["chat_id"] == 11:
                raise TelegramForbiddenError(method=NS(__returning__=None), message="blocked")
        telegram = NS(send_message=AsyncMock(side_effect=send))
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "get_verified_users_by_group", wraps=bot.adb.get_verified_users_by_group) as recipients, \
             patch.object(bot, "safe_edit_message", AsyncMock()) as edit, \
             patch.object(bot.asyncio, "sleep", AsyncMock()):
            await bot.cb_admin_group_notice_send(callback, self.state, telegram)
            recipients.assert_awaited_once_with("ТТ-36")
            self.assertEqual({call.kwargs['chat_id'] for call in telegram.send_message.await_args_list}, {10, 11})
            self.assertIn("Успешно: <b>1</b>", edit.await_args.args[1])
            self.assertIn("Не удалось: <b>1</b>", edit.await_args.args[1])

    async def test_confirm_button_is_one_shot(self):
        await self.state.update_data(group_notice_token="one-shot", group_notice_group="ТТ-36", group_notice_body="Update")
        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.adb, "get_verified_users_by_group", AsyncMock(return_value=[{"user_id": 10}])), \
             patch.object(bot, "safe_edit_message", AsyncMock()), \
             patch.object(bot.asyncio, "sleep", AsyncMock()):
            tg = NS(send_message=AsyncMock())
            await asyncio.gather(bot.cb_admin_group_notice_send(self.callback, self.state, tg),
                                 bot.cb_admin_group_notice_send(self.callback, self.state, tg))
            tg.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
