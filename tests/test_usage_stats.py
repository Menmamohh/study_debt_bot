import tempfile
import unittest
from pathlib import Path

import database as db


class UsageStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / "stats.db")
        db.init_db()
        db.add_user_request(10, None, "Student", "Student", "ТТ-36")
        db.set_user_status(10, "approved")
        db.add_user_request(11, None, "Student", "Student", "ХК-37")
        db.set_user_status(11, "pending")

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_only_approved_users_are_tracked_and_counted(self):
        db.record_usage_event(10, "schedule", "ТТ-36")
        db.record_usage_event(10, "schedule", "ТТ-36")
        db.record_usage_event(11, "useful", "ХК-37")
        stats = db.get_usage_stats(7)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["sections"], [{"section": "schedule", "opens": 2}])
        self.assertEqual(stats["groups"], [{"group_name": "ТТ-36", "active": 1}])

    def test_invalid_section_is_ignored(self):
        db.record_usage_event(10, "private_message", "ТТ-36")
        self.assertEqual(db.get_usage_stats(30)["active"], 0)


if __name__ == "__main__":
    unittest.main()
