import unittest

import keyboards as kb


class KeyboardTests(unittest.TestCase):
    @staticmethod
    def button_texts(markup):
        return [button.text for row in markup.inline_keyboard for button in row]

    @staticmethod
    def callback_data(markup):
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    def test_admin_panel_button_is_private_and_admin_only(self):
        student_menu = kb.main_menu_kb(is_admin_user=False)
        group_admin_menu = kb.main_menu_kb(is_admin_user=True, is_private_chat=False)
        admin_private_menu = kb.main_menu_kb(is_admin_user=True, is_private_chat=True)

        self.assertNotIn("admin_panel", self.callback_data(student_menu))
        self.assertNotIn("admin_panel", self.callback_data(group_admin_menu))
        self.assertIn("admin_panel", self.callback_data(admin_private_menu))

    def test_general_schedule_controls_are_admin_only(self):
        student_picker = kb.schedule_choose_group_kb(is_admin_user=False)
        admin_picker = kb.schedule_choose_group_kb(is_admin_user=True)
        student_schedule = kb.schedule_view_kb("ТТ-36", is_admin_user=False, has_general_photo=True)
        all_schedule = kb.schedule_view_kb("ALL", is_admin_user=True, is_user_own_group=False)

        self.assertNotIn("sched_view_ALL", self.callback_data(student_picker))
        self.assertIn("sched_view_ALL", self.callback_data(admin_picker))
        self.assertNotIn("adm_sched_edit_ТТ-36", self.callback_data(student_schedule))
        self.assertNotIn("adm_sched_clear_ТТ-36", self.callback_data(student_schedule))
        self.assertFalse(any("Сделать моей группой" in text for text in self.button_texts(all_schedule)))
        self.assertIn("👥 Другая группа", self.button_texts(student_schedule))

        other_group_schedule = kb.schedule_view_kb("ХК-37", is_admin_user=False, is_user_own_group=False, user_own_group="ТТ-36")
        self.assertFalse(any("Сделать моей группой" in text for text in self.button_texts(other_group_schedule)))
        self.assertIn("🔙 К расписанию ТТ-36", self.button_texts(other_group_schedule))

    def test_group_descriptors_are_omitted_from_group_selectors(self):
        labels = self.button_texts(kb.schedule_choose_group_kb())
        labels += self.button_texts(kb.select_group_kb())

        self.assertIn("ТТ-36", labels)
        self.assertIn("СР-38", labels)
        self.assertFalse(any("Мебель" in label or "Реставрация" in label for label in labels))

    def test_long_subject_names_use_short_callback_payloads(self):
        name = "Математика " * 30
        markup = kb.materials_hub_kb([{"name": name, "count": 0}])
        callback_data = markup.inline_keyboard[0][0].callback_data

        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)
        self.assertEqual(callback_data, f"b_sub_{kb.subject_callback_token(name)}")

    def test_long_subject_button_labels_are_truncated(self):
        markup = kb.subjects_list_kb([{"id": 1, "name": "Mathematics " * 20, "pending_tasks": 0}])

        self.assertLessEqual(len(markup.inline_keyboard[0][0].text), 48)

    def test_long_material_titles_do_not_make_oversized_buttons(self):
        markup = kb.materials_list_kb(1, 2, [{
            "id": 3,
            "material_type": "solution",
            "title": "Очень длинное название материала " * 20,
        }])

    def test_main_menu_tt36_vs_other_groups(self):
        tt36_menu = kb.main_menu_kb(student_group="ТТ-36", is_verified=True)
        xk_menu = kb.main_menu_kb(student_group="ХК-37", is_verified=True)
        other_menu = kb.main_menu_kb(student_group="ВД-65", is_verified=True)

        tt36_cbs = self.callback_data(tt36_menu)
        xk_cbs = self.callback_data(xk_menu)
        other_cbs = self.callback_data(other_menu)

        for cbs in (tt36_cbs, xk_cbs):
            self.assertIn("debts_hub", cbs)
            self.assertIn("browse_materials_hub", cbs)
            self.assertIn("quick_upload_material", cbs)
            self.assertIn("schedule_menu", cbs)
            self.assertNotIn("sched_bells", cbs)

        self.assertNotIn("debts_hub", other_cbs)
        self.assertNotIn("browse_materials_hub", other_cbs)
        self.assertNotIn("quick_upload_material", other_cbs)
        self.assertIn("schedule_menu", other_cbs)
        self.assertIn("sched_bells", other_cbs)


if __name__ == "__main__":
    unittest.main()
