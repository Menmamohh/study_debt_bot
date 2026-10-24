import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import bot
import database as db


class BotHandlerTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.original_admin_id = bot.ADMIN_ID
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.temp_dir.name) / "bot-test.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        bot.ADMIN_ID = self.original_admin_id
        self.temp_dir.cleanup()

    def test_bells_command_builds_markup(self):
        sent = {}

        class FakeMessage:
            from_user = SimpleNamespace(id=10)

            async def answer(self, text, **kwargs):
                sent["text"] = text
                sent.update(kwargs)

        asyncio.run(bot.cmd_bells(FakeMessage()))

        self.assertEqual(sent["text"], db.BELLS_SCHEDULE_TEXT)
        self.assertIsNotNone(sent["reply_markup"])
        self.assertIn("parse_mode", sent)

    def test_unknown_predefined_group_is_rejected(self):
        answers = []

        class FakeCallback:
            data = "grp_<invalid>"

            async def answer(self, *args, **kwargs):
                answers.append((args, kwargs))

        asyncio.run(bot.cb_select_group(FakeCallback(), state=None))

        self.assertTrue(answers)
        self.assertTrue(answers[0][1].get("show_alert"))
        self.assertEqual(db.get_pending_users(), [])

    def test_toggle_task_renders_the_updated_state(self):
        user_id = 10
        db.add_user_request(user_id, "test", "Test", "Test", "ТТ-36")
        db.set_user_status(user_id, "approved")
        subject_id = db.add_subject(user_id, "Math")
        task_id = db.add_task(user_id, subject_id, "Exercise 1")
        rendered = {}

        class FakeMessage:
            chat = SimpleNamespace(type="private")
            message_id = 1

            async def edit_text(self, text, **kwargs):
                rendered["text"] = text
                rendered.update(kwargs)

        class FakeCallback:
            data = f"toggle_t_{task_id}_{subject_id}"
            from_user = SimpleNamespace(id=user_id)
            message = FakeMessage()

            async def answer(self, *args, **kwargs):
                rendered["answer"] = args

        asyncio.run(bot.cb_toggle_task(FakeCallback()))

        self.assertIn("1/1 сдано", rendered["text"])
        self.assertIn("✅ Exercise 1", rendered["reply_markup"].inline_keyboard[0][0].text)

    def test_admin_review_queue_edits_the_message(self):
        admin_id = 99
        bot.ADMIN_ID = admin_id
        rendered = {}

        class FakeMessage:
            chat = SimpleNamespace(type="private")

            async def edit_text(self, text, **kwargs):
                rendered["text"] = text
                rendered.update(kwargs)

        class FakeCallback:
            data = "admin_review_queue"
            from_user = SimpleNamespace(id=admin_id, username="admin", full_name="Admin")
            message = FakeMessage()

            async def answer(self, *args, **kwargs):
                rendered["answer"] = args

        asyncio.run(bot.cb_admin_review_queue(FakeCallback(), bot=object()))

        self.assertIn("Админка › Материалы", rendered["text"])
        self.assertEqual(rendered["answer"], ("Очередь загружена!",))

    def test_admin_panel_is_not_opened_in_a_group_chat(self):
        admin_id = 99
        bot.ADMIN_ID = admin_id
        answers = []
        test_case = self

        class FakeMessage:
            chat = SimpleNamespace(type="group")

            async def edit_text(self, *args, **kwargs):
                test_case.fail("The admin panel must not be sent to a group chat")

        class FakeCallback:
            from_user = SimpleNamespace(id=admin_id)
            message = FakeMessage()

            async def answer(self, *args, **kwargs):
                answers.append((args, kwargs))

        asyncio.run(bot.cb_admin_panel(FakeCallback(), bot=object()))

        self.assertTrue(answers[0][1].get("show_alert"))

    def test_full_replacement_sheet_uses_saved_photo(self):
        db.set_group_schedule("ALL", "Full replacement sheet", photo_file_id="source-photo-id")
        sent = {}

        class FakeBot:
            async def send_photo(self, **kwargs):
                sent.update(kwargs)
                return SimpleNamespace(message_id=500)

            async def delete_message(self, **kwargs):
                pass

        class FakeCallback:
            message = SimpleNamespace(message_id=100, chat=SimpleNamespace(id=10, type="private"))
            from_user = SimpleNamespace(id=10)
            bot = FakeBot()

            async def answer(self, *args, **kwargs):
                sent["answered"] = True

        asyncio.run(bot.cb_schedule_view_gen_photo(FakeCallback()))

        self.assertEqual(sent["photo"], "source-photo-id")
        self.assertIn("Full replacement sheet", sent["caption"])

    def test_full_replacement_sheet_falls_back_to_source_photo_album(self):
        sent = {}

        class FakeBot:
            async def send_media_group(self, **kwargs):
                sent["media"] = kwargs["media"]

            async def send_message(self, **kwargs):
                sent["message"] = kwargs

        class FakeCallback:
            from_user = SimpleNamespace(id=10)
            bot = FakeBot()

            async def answer(self, *args, **kwargs):
                sent["answered"] = True

        asyncio.run(bot.cb_schedule_view_gen_photo(FakeCallback()))

        self.assertEqual(len(sent["media"]), 3)
        self.assertIn("19.10.2026", sent["media"][0].caption)
        self.assertIn("консультации", sent["media"][2].caption)
        self.assertIn("фотографии листов замен", sent["message"]["text"])
        self.assertIsNotNone(sent["message"]["reply_markup"])

    def test_stale_subject_delete_callback_is_rejected(self):
        user_id = 10
        db.add_user_request(user_id, "test", "Test", "Test", "ТТ-36")
        db.set_user_status(user_id, "approved")
        answers = []

        class FakeCallback:
            data = "del_sub_999"
            from_user = SimpleNamespace(id=user_id)

            async def answer(self, *args, **kwargs):
                answers.append((args, kwargs))

        asyncio.run(bot.cb_del_sub(FakeCallback()))

        self.assertTrue(answers[0][1].get("show_alert"))

    def test_non_tt36_student_is_denied_debts_and_materials(self):
        user_id = 25
        db.add_user_request(user_id, "vd_user", "VD Student", "VD Student", "ВД-65")
        db.set_user_status(user_id, "approved")
        rendered = {}

        class FakeMessage:
            chat = SimpleNamespace(type="private")
            message_id = 1

            async def edit_text(self, text, **kwargs):
                rendered["text"] = text
                rendered.update(kwargs)

        class FakeCallback:
            data = "debts_hub"
            from_user = SimpleNamespace(id=user_id)
            message = FakeMessage()

            async def answer(self, *args, **kwargs):
                rendered["answer"] = args

        async def fake_clear():
            pass

        asyncio.run(bot.cb_debts_hub(FakeCallback(), state=SimpleNamespace(clear=fake_clear)))
        self.assertIn("настроены для групп <b>ТТ-36</b> и <b>ХК</b>", rendered["text"])
        self.assertIn("ВД-65", rendered["text"])

        # Also test browse_materials_hub
        rendered.clear()
        FakeCallback.data = "browse_materials_hub"
        asyncio.run(bot.cb_browse_materials_hub(FakeCallback(), state=SimpleNamespace(clear=fake_clear)))
        self.assertIn("настроены для групп <b>ТТ-36</b> и <b>ХК</b>", rendered["text"])

    def test_xk_student_has_access_to_debts_and_materials(self):
        user_id = 26
        db.add_user_request(user_id, "xk_user", "XK Student", "XK Student", "ХК-37")
        db.set_user_status(user_id, "approved")
        rendered = {}

        class FakeMessage:
            chat = SimpleNamespace(type="private")
            message_id = 1

            async def edit_text(self, text, **kwargs):
                rendered["text"] = text
                rendered.update(kwargs)

        class FakeCallback:
            data = "debts_hub"
            from_user = SimpleNamespace(id=user_id)
            message = FakeMessage()

            async def answer(self, *args, **kwargs):
                rendered["answer"] = args

        async def fake_clear():
            pass

        asyncio.run(bot.cb_debts_hub(FakeCallback(), state=SimpleNamespace(clear=fake_clear)))
        self.assertIn("Моя учёба", rendered["text"])

        rendered.clear()
        FakeCallback.data = "browse_materials_hub"
        asyncio.run(bot.cb_browse_materials_hub(FakeCallback(), state=SimpleNamespace(clear=fake_clear)))
        self.assertIn("Методички и решения (ХК-37)", rendered["text"])

    def test_stale_admin_material_delete_callback_is_rejected(self):
        user_id = 10
        subject_id = db.add_subject(user_id, "Math")
        task_id = db.add_task(user_id, subject_id, "Exercise 1")
        material_id = db.add_material(user_id, "student", task_id, "solution", "Answer")
        db.delete_material(material_id)
        admin_id = 99
        bot.ADMIN_ID = admin_id
        answers = []

        class FakeCallback:
            data = f"adm_do_del_{material_id}"
            from_user = SimpleNamespace(id=admin_id)

            async def answer(self, *args, **kwargs):
                answers.append((args, kwargs))

        asyncio.run(bot.cb_admin_do_delete_material(FakeCallback()))

        self.assertTrue(answers[0][1].get("show_alert"))
        self.assertIn("уже удален", answers[0][0][0])

    def test_start_verify_flow_edits_caption_on_photo_message(self):
        rendered = {}

        class FakePhotoMessage:
            photo = [SimpleNamespace(file_id="photo123")]

            async def edit_caption(self, caption, **kwargs):
                rendered["caption"] = caption
                rendered.update(kwargs)

            async def edit_text(self, text, **kwargs):
                raise Exception("Bad Request: there is no text in the message to edit")

        class FakeCallback:
            data = "start_verify_flow"
            from_user = SimpleNamespace(id=50)
            message = FakePhotoMessage()

            async def answer(self, *args, **kwargs):
                rendered["answered"] = True

        async def fake_clear():
            pass

        asyncio.run(bot.cb_start_verify_flow(FakeCallback(), state=SimpleNamespace(clear=fake_clear)))
        self.assertIn("Выбери свою учебную группу", rendered["caption"])
        self.assertTrue(rendered.get("answered"))
        self.assertIsNotNone(rendered.get("reply_markup"))

    def test_bottom_start_keyboard_and_trigger(self):
        import keyboards as kb
        markup = kb.bottom_start_kb()
        button_texts = [b.text for row in markup.keyboard for b in row]
        self.assertIn("🚀 Начать", button_texts)


if __name__ == "__main__":
    unittest.main()
