import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database as db
import gdrive_sync


class ScheduleHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / "history.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_manual_versions_album_dedup_restart_and_deletion(self):
        db.set_group_schedule("ALL", "Tuesday", photo_file_ids=["a", "b"])
        with db.get_connection() as conn:
            conn.execute("UPDATE schedule_versions SET created_at = '2020-01-01 00:00:00'")
        first = db.get_schedule_versions("ТТ-36")[0]
        db.set_group_schedule("ALL", "Wednesday", photo_file_ids=["c", "d"])
        db.set_group_schedule("ALL", "Thursday", photo_file_ids=["e", "f"])
        self.assertFalse(db.set_group_schedule("ALL", "Thursday", photo_file_ids=["e", "f"]))
        db.init_db()  # Reopening the DB must keep history, without any time expiry.
        versions = db.get_schedule_versions("ТТ-36")
        self.assertEqual(len(versions), 3)
        self.assertEqual(versions[-1], first)
        self.assertEqual([p["source"] for p in first["payload"]["photos"]], ["a", "b"])
        self.assertFalse(db.delete_old_schedule_version(versions[0]["id"]))
        self.assertTrue(db.delete_old_schedule_version(first["id"]))
        self.assertFalse(db.delete_old_schedule_version(first["id"]))
        self.assertEqual(len(db.get_schedule_versions("ХК-37")), 2)

    def test_group_scope_and_shared_publications(self):
        db.set_group_schedule("ТТ-36", "Own group")
        own_id = db.get_schedule_versions("ТТ-36")[0]["id"]
        self.assertEqual(db.get_schedule_versions("ХК-37"), [])
        db.set_group_schedule("ALL", "All groups")
        self.assertEqual(db.get_schedule_versions("ТТ-36")[0]["payload"]["schedule_text"], "All groups")
        self.assertTrue(db.delete_old_schedule_version(own_id))

    def test_drive_updates_keep_original_bytes_and_noop_sync_keeps_manual_current(self):
        item = {"file_id": "test_one", "filename": "замена 20-10.jpeg", "doc_type": "replacement",
                "schedule_date": "2026-10-20", "part_number": 1, "file_size": 3, "drive_timestamp": 1}
        content = [b"old"]

        def download(file_id, path):
            Path(path).write_bytes(content[0])
            return True

        with patch.object(gdrive_sync, "DOWNLOADS_DIR", str(Path(self.temp.name) / "downloads")), \
                patch.object(gdrive_sync, "fetch_drive_files_list", return_value=[item]), \
                patch.object(gdrive_sync, "download_file", side_effect=download):
            gdrive_sync.sync_drive_schedule()
            first = db.get_schedule_versions("ALL")[0]
            content[0] = b"new"
            item["drive_timestamp"] = 2
            gdrive_sync.sync_drive_schedule()
            self.assertEqual(len(db.get_schedule_versions("ALL")), 2)
            self.assertEqual(Path(first["payload"]["photos"][0]["source"]).read_bytes(), b"old")
            gdrive_sync.sync_drive_schedule(force=True)
            self.assertEqual(len(db.get_schedule_versions("ALL")), 2)
            db.set_group_schedule("ALL", "Manual override")
            gdrive_sync.sync_drive_schedule()
            self.assertEqual(db.get_schedule_versions("ALL")[0]["payload"]["schedule_text"], "Manual override")
            content[0] = b"newest"
            item["drive_timestamp"] = 3
            gdrive_sync.sync_drive_schedule()
            self.assertEqual(db.get_schedule_versions("ALL")[0]["payload"]["title"], "Листы замен и консультаций")

    def test_new_date_becomes_current_immediately_and_old_date_stays(self):
        for day in (20, 21):
            path = Path(self.temp.name) / f"{day}.jpg"
            path.write_bytes(str(day).encode())
            db.save_gdrive_file(str(day), path.name, "replacement", f"2026-10-{day}", local_path=str(path))
            db.record_schedule_version("ALL", gdrive_sync.drive_schedule_payload())
        versions = db.get_schedule_versions("ALL")
        self.assertIn("2026-10-21", versions[0]["payload"]["photos"][0]["caption"])
        self.assertIn("2026-10-20", versions[1]["payload"]["photos"][0]["caption"])

    def test_album_collected_once_in_order(self):
        async def scenario():
            state = SimpleNamespace(get_state=AsyncMock(return_value=bot.Form.waiting_for_group_schedule.state))
            middleware = bot.ScheduleAlbumMiddleware(settle_seconds=0.01)
            handler = AsyncMock()
            def message(number):
                return SimpleNamespace(media_group_id="album", message_id=number,
                                       chat=SimpleNamespace(id=1, type="private"), from_user=SimpleNamespace(id=1))
            with patch.object(bot, "is_admin", return_value=True):
                await asyncio.gather(middleware(handler, message(3), {"state": state}),
                                     middleware(handler, message(1), {"state": state}),
                                     middleware(handler, message(2), {"state": state}))
            self.assertEqual(handler.await_count, 1)
            self.assertEqual([m.message_id for m in handler.call_args.args[1]["schedule_album"]], [1, 2, 3])
            self.assertFalse(middleware.pending)
        asyncio.run(scenario())

    def test_cancel_while_album_arrives_does_not_publish(self):
        async def scenario():
            state = SimpleNamespace(get_state=AsyncMock(side_effect=[bot.Form.waiting_for_group_schedule.state, None]))
            event = SimpleNamespace(media_group_id="cancelled", message_id=1,
                                    chat=SimpleNamespace(id=1, type="private"), from_user=SimpleNamespace(id=1))
            handler = AsyncMock()
            middleware = bot.ScheduleAlbumMiddleware(settle_seconds=0.001)
            with patch.object(bot, "is_admin", return_value=True):
                await middleware(handler, event, {"state": state})
            handler.assert_not_awaited()
            self.assertFalse(middleware.pending)
        asyncio.run(scenario())

    def test_manual_album_waits_for_confirmation_and_keeps_caption(self):
        async def scenario():
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey
            from aiogram.fsm.storage.memory import MemoryStorage
            state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
            await state.update_data(target_group="SELECT")
            def message(number, caption=None):
                return SimpleNamespace(photo=[SimpleNamespace(file_id=f"photo{number}")], caption=caption,
                                       text=None, chat=SimpleNamespace(type="private", id=1),
                                       from_user=SimpleNamespace(id=1, username="admin"))
            album = [message(1), message(2, "Caption on second photo"), message(3)]
            sender = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=201), SimpleNamespace(message_id=202)]), send_message=AsyncMock())
            with patch.object(bot, "is_admin", return_value=True), \
                    patch.object(bot, "show_user_screen", AsyncMock()):
                await bot.process_group_schedule_input(album[0], state, sender, schedule_album=album)
            payload = await state.get_data()
            self.assertEqual(payload["schedule_body"], "Caption on second photo")
            self.assertEqual(payload["schedule_photos"], ["photo1", "photo2", "photo3"])
            self.assertFalse(payload["schedule_all"])
            self.assertEqual(db.get_schedule_versions("ALL"), [])
            self.assertEqual(len(sender.send_media_group.call_args.kwargs["media"]), 3)
            self.assertEqual(sender.send_media_group.await_count, 1)  # Admin preview only.
        asyncio.run(scenario())

    def test_view_old_version_is_local_and_sends_all_photos(self):
        async def scenario():
            db.set_group_schedule("ALL", "Old", photo_file_ids=["p1", "p2"])
            db.set_group_schedule("ALL", "New")
            versions = db.get_schedule_versions("ALL")
            sender = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]),
                                     send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)))
            target = SimpleNamespace(bot=sender, chat=SimpleNamespace(type="private", id=10))
            with patch.object(bot, "is_admin", return_value=False), \
                    patch.object(bot, "cleanup_user_ui_messages", AsyncMock()):
                await bot.render_group_schedule_view(target, SimpleNamespace(id=10), "ALL", version_id=versions[1]["id"])
            self.assertEqual(len(sender.send_media_group.call_args.kwargs["media"]), 2)
            self.assertIn("Old", sender.send_message.call_args.kwargs["text"])
            self.assertEqual(db.get_schedule_versions("ALL")[0]["payload"]["schedule_text"], "New")
        asyncio.run(scenario())

    def test_students_cannot_delete_and_stale_deleted_buttons_show_current(self):
        async def scenario():
            db.set_group_schedule("ALL", "Old")
            old_id = db.get_schedule_versions("ALL")[0]["id"]
            db.set_group_schedule("ALL", "New")
            state = SimpleNamespace(clear=AsyncMock())
            callback = SimpleNamespace(data=f"schdel_{old_id}_ALL", from_user=SimpleNamespace(id=10),
                                       message=SimpleNamespace(chat=SimpleNamespace(type="private")), answer=AsyncMock())
            with patch.object(bot, "check_user_access", AsyncMock(return_value=(True, ""))), \
                    patch.object(bot, "is_admin", return_value=False):
                await bot.cb_schedule_history(callback, state)
            self.assertEqual(len(db.get_schedule_versions("ALL")), 2)
            db.delete_old_schedule_version(old_id)
            with patch.object(bot, "render_schedule_version", AsyncMock()) as render:
                await bot.render_group_schedule_view(callback, callback.from_user, "ALL", is_callback=True, version_id=old_id)
                self.assertEqual(render.call_args.args[4]["payload"]["schedule_text"], "New")
                self.assertIn("удалено", render.call_args.kwargs["notice"])
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
