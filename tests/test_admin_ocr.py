import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database as db
import keyboards as kb


class AdminOcrTests(unittest.TestCase):
    def setUp(self):
        self.original_admin_id = bot.ADMIN_ID
        bot.ADMIN_ID = 99

    def tearDown(self):
        bot.ADMIN_ID = self.original_admin_id

    def test_admin_panel_has_ocr_check_and_group_selector(self):
        panel = kb.admin_section_kb("tools")
        self.assertTrue(any(button.callback_data == "adm_ocr_mode"
                            for row in panel.inline_keyboard for button in row))
        selector = kb.admin_ocr_groups_kb(db.COLLEGE_GROUPS)
        callbacks = [button.callback_data for row in selector.inline_keyboard for button in row]
        self.assertIn("adm_ocr_group_ТТ-36", callbacks)

    def test_group_check_shows_ocr_and_private_source_without_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            image_path = Path(folder) / "replacement.jpeg"
            image_path.write_bytes(b"fake image path for mocked send")
            rendered = {}

            async def capture_screen(_callback, text, **kwargs):
                rendered["text"] = text
                rendered["markup"] = kwargs.get("reply_markup")

            class FakeBot:
                async def send_photo(self, **kwargs):
                    rendered["photo"] = kwargs
                    return SimpleNamespace(message_id=500)

            class FakeCallback:
                data = "adm_ocr_group_ТТ-36"
                from_user = SimpleNamespace(id=99)
                message = SimpleNamespace(chat=SimpleNamespace(type="private"))
                bot = FakeBot()

                async def answer(self, *args, **kwargs):
                    rendered["callback_answer"] = args

            files = [{
                "local_path": str(image_path),
                "filename": "замены.jpeg",
                "schedule_date": "2026-10-20",
            }]
            with patch.object(bot, "show_user_screen", new=AsyncMock(side_effect=capture_screen)), \
                    patch.object(bot.db, "get_latest_gdrive_schedule_files", return_value=files) as get_files, \
                    patch.object(bot.gdrive_sync, "get_target_study_date", return_value=(datetime.date(2026, 10, 21), False, "")), \
                    patch.object(bot.gdrive_sync, "_schedule_group_markers", return_value=(900, 1200, (("ТТ-36", 100),))), \
                    patch.object(bot.gdrive_sync, "_ocr_schedule_group", return_value="Пара: технология"), \
                    patch.object(bot.adb, "set_ui_message_id", new=AsyncMock()):
                asyncio.run(bot.cb_admin_ocr_group(FakeCallback()))
                self.assertEqual(get_files.call_args.kwargs, {"doc_type": "replacement"})

            self.assertIn("Пара: технология", rendered["text"])
            self.assertIn("Проверка для администратора", rendered["text"])
            self.assertNotIn("Найдены группы", rendered["text"])
            self.assertIn("2026-10-20", rendered["text"])
            self.assertEqual(rendered["photo"]["chat_id"], 99)
            buttons = [button.callback_data for row in rendered["markup"].inline_keyboard for button in row]
            self.assertNotIn("sr_publish_", " ".join(buttons))


if __name__ == "__main__":
    unittest.main()
