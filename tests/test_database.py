import tempfile
import unittest
from pathlib import Path

import database as db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()
        db.DB_PATH = self.original_db_path

    def test_foreign_keys_are_enabled_and_materials_cascade(self):
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

        subject_id = db.add_subject(10, "Woodwork")
        task_id = db.add_task(10, subject_id, "Lab 1")
        material_id = db.add_material(10, "student", task_id, "method", "Guide")
        self.assertIsNotNone(material_id)

        db.delete_subject(10, subject_id)

        self.assertIsNone(db.get_material_by_id(material_id))

    def test_task_and_material_creation_require_ownership(self):
        subject_id = db.add_subject(10, "Math")
        task_id = db.add_task(10, subject_id, "Exercise 1")

        self.assertIsNone(db.add_task(20, subject_id, "Foreign task"))
        self.assertIsNone(db.add_material(20, "other", task_id, "solution", "Foreign material"))

    def test_subject_names_are_case_insensitively_unique_per_user(self):
        self.assertIsNotNone(db.add_subject(10, "Mathematics"))
        self.assertIsNone(db.add_subject(10, "mathematics"))
        self.assertIsNotNone(db.add_subject(20, "mathematics"))

    def test_empty_summary_has_zero_progress(self):
        summary = db.get_all_debts_summary(10)

        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["percent"], 0)
        self.assertEqual(summary["bar"], "░" * 10)

    def test_summary_tracks_task_completion(self):
        subject_id = db.add_subject(10, "Math")
        task_id = db.add_task(10, subject_id, "Exercise 1")

        self.assertEqual(db.get_all_debts_summary(10)["pending"], 1)
        self.assertTrue(db.toggle_task_status(10, task_id))
        summary = db.get_all_debts_summary(10)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["percent"], 100)

    def test_subject_task_pages_include_global_counts_and_toggle_atomically(self):
        subject_id = db.add_subject(10, "Math")
        task_ids = [db.add_task(10, subject_id, f"Exercise {index}") for index in range(3)]

        page = db.get_subject_tasks_page(10, subject_id, limit=2, offset=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["pending"], 3)
        self.assertEqual([task["id"] for task in page["tasks"]], task_ids[1:])

        status, tasks, total, pending = db.toggle_task_status_and_get_tasks(
            10, subject_id, task_ids[0], limit=2, offset=0
        )
        self.assertTrue(status)
        self.assertEqual((total, pending), (3, 2))
        self.assertEqual([task["id"] for task in tasks], task_ids[1:])

    def test_task_lookup_is_scoped_to_requesting_subject_owner(self):
        subject_id = db.add_subject(10, "Math")
        task_id = db.add_task(10, subject_id, "Exercise 1")

        self.assertIsNotNone(db.get_task_info_for_subject(10, subject_id, task_id))
        self.assertIsNone(db.get_task_info_for_subject(20, subject_id, task_id))

    def test_seed_default_subjects_for_tt36_and_xk_groups(self):
        # User in TT-36
        db.add_user_request(100, "tt_user", "TT User", "TT User", "ТТ-36")
        db.seed_default_subjects(100)
        tt_subs = db.get_subjects_with_counts(100)
        self.assertEqual(len(tt_subs), 8)
        self.assertEqual(len(tt_subs), len(db.DEFAULT_SUBJECTS_BY_GROUP["ТТ-36"]))

        # User in XK-37
        db.add_user_request(150, "xk_user", "XK User", "XK User", "ХК-37")
        db.seed_default_subjects(150)
        xk_subs = db.get_subjects_with_counts(150)
        self.assertEqual(len(xk_subs), 13)
        self.assertEqual(len(xk_subs), len(db.DEFAULT_SUBJECTS_BY_GROUP["ХК"]))
        xk_sub_names = [s["name"] for s in xk_subs]
        self.assertIn("Компьютерная графика", xk_sub_names)
        self.assertIn("Прикладная информатика", xk_sub_names)
        self.assertIn("Основы экономики и предпринимательской деятельности", xk_sub_names)

        # User in XK-38
        db.add_user_request(160, "xk38_user", "XK38 User", "XK38 User", "ХК-38")
        db.seed_default_subjects(160)
        self.assertEqual(len(db.get_subjects_with_counts(160)), 13)

        # User in non-supported group
        db.add_user_request(200, "vd_user", "VD User", "VD User", "ВД-65")
        db.seed_default_subjects(200)
        self.assertEqual(len(db.get_subjects_with_counts(200)), 0)

    def test_reset_all_verifications_preserves_admin_only(self):
        admin_id = 1
        db.set_admin_id(admin_id)
        db.ensure_admin_user(admin_id, "Menmamoh")
        db.add_user_request(100, "student1", "Student 1", "Student 1", "ТТ-36")
        db.set_user_status(100, "approved")
        db.add_user_request(200, "student2", "Student 2", "Student 2", "ХК-37")

        sub_id = db.add_subject(100, "Test Sub")
        db.add_task(100, sub_id, "Test Task")

        deleted = db.reset_all_verifications(admin_id)
        self.assertEqual(deleted, 2)

        # Admin must be intact
        admin = db.get_user(admin_id)
        self.assertIsNotNone(admin)
        self.assertEqual(admin["status"], "approved")

        # Other users must be deleted
        self.assertIsNone(db.get_user(100))
        self.assertIsNone(db.get_user(200))
        self.assertEqual(len(db.get_subjects_with_counts(100)), 0)

    def test_tt36_never_mixes_with_xk_subjects(self):
        user_id = 777
        db.add_user_request(user_id, "tt_stud", "TT Student", "TT Student", "ТТ-36")
        db.seed_default_subjects(user_id)

        # Accidental manual insert of XK subjects should be purged or excluded
        with db.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO subjects (user_id, name) VALUES (?, ?)", (user_id, "Компьютерная графика"))

        # Calling seed_default_subjects purges foreign default subjects
        db.seed_default_subjects(user_id)

        subs = db.get_subjects_with_counts(user_id)
        sub_names = [s["name"] for s in subs]
        self.assertEqual(len(sub_names), 8)
        self.assertNotIn("Компьютерная графика", sub_names)
        self.assertNotIn("Шрифты", sub_names)

        known = db.get_all_known_subject_names(user_id)
        self.assertNotIn("Компьютерная графика", known)
        self.assertNotIn("Шрифты", known)
        self.assertEqual(len(known), 8)


if __name__ == "__main__":
    unittest.main()
