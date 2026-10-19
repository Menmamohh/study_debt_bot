import os
import unittest
import datetime

import database as db
import gdrive_sync


class TestGDriveSync(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def tearDown(self):
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM gdrive_schedule_files WHERE file_id LIKE 'test_%'")
            conn.commit()

    def test_parse_filename_meta(self):
        meta1 = gdrive_sync.parse_filename_meta("замена понедельник 19-10-2.jpeg")
        self.assertEqual(meta1["doc_type"], "replacement")
        self.assertEqual(meta1["part_number"], 2)
        self.assertIn("10-19", meta1["schedule_date"])

        meta2 = gdrive_sync.parse_filename_meta("замена понедельник 19-10.jpeg")
        self.assertEqual(meta2["doc_type"], "replacement")
        self.assertEqual(meta2["part_number"], 1)
        self.assertIn("10-19", meta2["schedule_date"])

        meta3 = gdrive_sync.parse_filename_meta("замена суббота 17-10.jpeg")
        self.assertEqual(meta3["doc_type"], "replacement")
        self.assertEqual(meta3["part_number"], 1)
        self.assertIn("10-17", meta3["schedule_date"])

        meta4 = gdrive_sync.parse_filename_meta("консультации понедельник 19-10.jpeg")
        self.assertEqual(meta4["doc_type"], "consultations")
        self.assertEqual(meta4["part_number"], 1)

    def test_database_gdrive_files(self):
        test_file_id = "test_fid_12345"
        db.save_gdrive_file(
            file_id=test_file_id,
            filename="замена вторник 20-10.jpeg",
            doc_type="replacement",
            schedule_date="2026-10-20",
            part_number=1,
            local_path="/tmp/fake_sched.jpeg",
            file_size=1024,
            drive_timestamp=123456789
        )
        file_info = db.get_gdrive_file(test_file_id)
        self.assertIsNotNone(file_info)
        self.assertEqual(file_info["filename"], "замена вторник 20-10.jpeg")
        self.assertEqual(file_info["part_number"], 1)
        self.assertEqual(file_info["schedule_date"], "2026-10-20")

    def test_active_photos_formatting(self):
        # Create a fake file on disk to test get_active_gdrive_schedule_photos
        fake_path = "/tmp/test_active_photo.jpeg"
        with open(fake_path, "wb") as f:
            f.write(b"fake jpeg data")

        try:
            db.save_gdrive_file(
                file_id="test_active_fid",
                filename="замена пятница 23-10.jpeg",
                doc_type="replacement",
                schedule_date="2026-10-23",
                part_number=1,
                local_path=fake_path,
                file_size=14,
                drive_timestamp=999999
            )
            photos = gdrive_sync.get_active_gdrive_schedule_photos(target_date=datetime.date(2026, 10, 23))
            self.assertTrue(len(photos) > 0)
            self.assertEqual(photos[0][0], fake_path)
            self.assertIn("23.10.2026", photos[0][1])
        finally:
            if os.path.exists(fake_path):
                os.remove(fake_path)


    def test_get_target_study_date_weekdays(self):
        tz = gdrive_sync.TIMEZONE
        # Пн 10:00 (утро) -> сегодня (Пн)
        mon_morning = datetime.datetime(2026, 10, 19, 10, 0, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(mon_morning)
        self.assertEqual(target, datetime.date(2026, 10, 19))
        self.assertFalse(is_next)

        # Пн 17:05 (вечер после отсечки) -> завтра (Вт)
        mon_evening = datetime.datetime(2026, 10, 19, 17, 5, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(mon_evening)
        self.assertEqual(target, datetime.date(2026, 10, 20))
        self.assertTrue(is_next)

        # Пт 17:30 -> суббота
        fri_evening = datetime.datetime(2026, 10, 23, 17, 30, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(fri_evening)
        self.assertEqual(target, datetime.date(2026, 10, 24))
        self.assertTrue(is_next)

    def test_get_target_study_date_weekend(self):
        tz = gdrive_sync.TIMEZONE
        # Сб 10:00 (утро во время пар) -> сегодня (Сб)
        sat_morning = datetime.datetime(2026, 10, 17, 10, 0, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(sat_morning)
        self.assertEqual(target, datetime.date(2026, 10, 17))
        self.assertFalse(is_next)

        # Сб 14:15 (пары окончены) -> понедельник
        sat_afternoon = datetime.datetime(2026, 10, 17, 14, 15, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(sat_afternoon)
        self.assertEqual(target, datetime.date(2026, 10, 19))
        self.assertTrue(is_next)

        # Сб 17:00 -> понедельник
        sat_evening = datetime.datetime(2026, 10, 17, 17, 0, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(sat_evening)
        self.assertEqual(target, datetime.date(2026, 10, 19))
        self.assertTrue(is_next)

        # Вс 12:00 -> понедельник
        sunday = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=tz)
        target, is_next, _ = gdrive_sync.get_target_study_date(sunday)
        self.assertEqual(target, datetime.date(2026, 10, 19))
        self.assertTrue(is_next)

    def test_past_date_not_taken(self):
        # Target date is Monday 19.10
        target_mon = datetime.date(2026, 10, 19)
        photos = gdrive_sync.get_active_gdrive_schedule_photos(target_date=target_mon)
        # Verify no file from 17.10 (Saturday) is present
        for path, caption, valid_until in photos:
            self.assertGreaterEqual(valid_until, target_mon)
            self.assertNotIn("17.10", caption)


if __name__ == "__main__":
    unittest.main()

