from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from typing import List, Dict, Optional
import hashlib

def subject_callback_token(subject_name: str) -> str:
    """Short, stable callback payload for arbitrary subject names."""
    return hashlib.sha256(subject_name.encode("utf-8")).hexdigest()[:32]

def _short_label(value: str, max_length: int = 48) -> str:
    return value if len(value) <= max_length else value[:max_length - 1] + "…"

def main_menu_kb(is_admin_user: bool = False, pending_count: int = 0, is_verified: bool = True, student_group: Optional[str] = None, is_private_chat: bool = True, verification_pending: bool = False, has_unread_notification: bool = False) -> InlineKeyboardMarkup:
    kb = []

    # Личный трекер, профиль и админ-раздел доступны только в личном чате.
    if not is_private_chat:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Расписание занятий", callback_data="schedule_menu")],
            [InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="sched_bells")],
        ])
    
    # Кнопка верификации для тех, кто зашёл раньше или ещё не подтверждён
    if not is_verified and not is_admin_user:
        if not verification_pending:
            kb.append([
                InlineKeyboardButton(text="🔐 Выбрать группу для доступа", callback_data="start_verify_flow")
            ])
        return InlineKeyboardMarkup(inline_keyboard=kb)
        
    has_study_features = (
        is_admin_user or 
        (bool(student_group) and (student_group == "ТТ-36" or student_group.strip().upper().startswith(("ХК", "ВД"))))
    )

    if has_study_features:
        kb.extend([
            [
                InlineKeyboardButton(text="📅 Расписание занятий", callback_data="schedule_menu")
            ],
            [
                InlineKeyboardButton(text="📝 Трекер долгов и работ", callback_data="debts_hub")
            ],
            [InlineKeyboardButton(text="💡 Полезное", callback_data="useful_menu")]
        ])
    else:
        kb.extend([
            [
                InlineKeyboardButton(text="📅 Расписание занятий", callback_data="schedule_menu")
            ],
            [InlineKeyboardButton(text="💡 Полезное", callback_data="useful_menu")]
        ])

    if has_unread_notification:
        kb.append([InlineKeyboardButton(text="🔔 Уведомление", callback_data="notification_open")])
    
    if is_admin_user and is_private_chat:
        badge = f" (⏳ {pending_count})" if pending_count > 0 else ""
        kb.append([
            InlineKeyboardButton(text=f"👑 Админ-панель{badge}", callback_data="admin_panel")
        ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def debts_hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📚 Мои предметы и работы", callback_data="list_subjects")
        ],
        [
            InlineKeyboardButton(text="📊 Показать все долги списком", callback_data="show_all_debts")
        ],
        [
            InlineKeyboardButton(text="➕ Добавить свой предмет", callback_data="add_subject_prompt")
        ],
        [InlineKeyboardButton(text="📖 База методичек и решений", callback_data="browse_materials_hub")],
        [InlineKeyboardButton(text="📤 Добавить методичку или решение", callback_data="quick_upload_material")],
        [
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
        ]
    ])

def useful_hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
        [InlineKeyboardButton(text="📄 Пример заявления", callback_data="useful_application")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])

def useful_application_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К разделу «Полезное»", callback_data="useful_menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])

def notification_message_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])

def back_to_debts_hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К трекеру долгов", callback_data="debts_hub")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def schedule_view_kb(
    group_name: str, 
    is_admin_user: bool = False, 
    has_general_photo: bool = False, 
    has_general_schedule: bool = False,
    is_user_own_group: bool = True,
    user_own_group: Optional[str] = None
) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sched_view_{group_name}")
        ]
    ]
    if not is_user_own_group and user_own_group and user_own_group != group_name and user_own_group != "ALL":
        kb.append([
            InlineKeyboardButton(text=f"🔙 К расписанию {user_own_group}", callback_data=f"sched_view_{user_own_group}")
        ])
    kb.append([
        InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="sched_bells")
    ])
    if is_admin_user:
        kb.append([
            InlineKeyboardButton(text=f"✏️ Изменить расписание ({group_name})", callback_data=f"adm_sched_edit_{group_name}"),
            InlineKeyboardButton(text="🗑️ Очистить", callback_data=f"adm_sched_clear_{group_name}")
        ])
        kb.append([
            InlineKeyboardButton(text="🔄 Синхронизация с Google Диском", callback_data="adm_gdrive_sync")
        ])
    kb.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def schedule_choose_group_kb(current_group: Optional[str] = None, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    kb = []
    if is_admin_user:
        kb.append([
            InlineKeyboardButton(text="🔄 Синхронизация с Google Диском", callback_data="adm_gdrive_sync")
        ])
        
    # Деревообработка / Мебель
    kb.append([
        InlineKeyboardButton(text="ТТ-36", callback_data="sched_view_ТТ-36"),
        InlineKeyboardButton(text="ТТ-37", callback_data="sched_view_ТТ-37"),
        InlineKeyboardButton(text="ТТ-38", callback_data="sched_view_ТТ-38"),
    ])
    # Черчение и труд
    kb.append([
        InlineKeyboardButton(text="ТТЧ-37", callback_data="sched_view_ТТЧ-37"),
        InlineKeyboardButton(text="ТТЧ-38", callback_data="sched_view_ТТЧ-38"),
    ])
    # Керамика
    kb.append([
        InlineKeyboardButton(text="ХК-37", callback_data="sched_view_ХК-37"),
        InlineKeyboardButton(text="ХК-38", callback_data="sched_view_ХК-38"),
        InlineKeyboardButton(text="ХК-39", callback_data="sched_view_ХК-39"),
    ])
    # Дошкольное образование
    kb.append([
        InlineKeyboardButton(text="ВД-65", callback_data="sched_view_ВД-65"),
        InlineKeyboardButton(text="ВД-66", callback_data="sched_view_ВД-66"),
        InlineKeyboardButton(text="ВД-67", callback_data="sched_view_ВД-67"),
    ])
    kb.append([
        InlineKeyboardButton(text="ВД-68", callback_data="sched_view_ВД-68"),
        InlineKeyboardButton(text="ВД-69", callback_data="sched_view_ВД-69"),
        InlineKeyboardButton(text="ВД-70", callback_data="sched_view_ВД-70"),
    ])
    # Строители и реставрация
    kb.append([
        InlineKeyboardButton(text="С-37", callback_data="sched_view_С-37"),
        InlineKeyboardButton(text="С-38", callback_data="sched_view_С-38"),
        InlineKeyboardButton(text="С-39", callback_data="sched_view_С-39"),
    ])
    kb.append([
        InlineKeyboardButton(text="СР-38", callback_data="sched_view_СР-38")
    ])
    
    if current_group:
        kb.append([
            InlineKeyboardButton(text=f"🔙 Назад к {current_group}", callback_data=f"sched_view_{current_group}")
        ])
    kb.append([
        InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="sched_bells"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# Сохраняем обратную совместимость
def schedule_menu_kb(selected_day: Optional[int] = None, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    return schedule_choose_group_kb(is_admin_user=is_admin_user)

def subjects_list_kb(subjects: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in subjects:
        pending = s["pending_tasks"]
        status_icon = "🔴" if pending > 0 else "🟢"
        text = _short_label(f"{status_icon} {s['name']} (долгов: {pending})")
        kb.append([InlineKeyboardButton(text=text, callback_data=f"open_sub_{s['id']}")])
    
    kb.append([
        InlineKeyboardButton(text="➕ Добавить предмет", callback_data="add_subject_prompt")
    ])
    kb.append([
        InlineKeyboardButton(text="🔙 К трекеру долгов", callback_data="debts_hub"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def subject_tasks_kb(
    subject_id: int,
    tasks: List[Dict],
    mat_counts: Optional[Dict[int, int]] = None,
    page: int = 0,
    total: Optional[int] = None,
    page_size: int = 25,
) -> InlineKeyboardMarkup:
    kb = []
    mat_counts = mat_counts or {}
    
    for t in tasks:
        icon = "✅" if t["is_done"] else "❌"
        count = mat_counts.get(t["id"], 0)
        mat_btn_label = f"📚 ({count})" if count > 0 else "📚 Добавить"
        
        # Слева — кнопка переключения сдал/долг, справа — материалы/методички
        kb.append([
            InlineKeyboardButton(
                text=_short_label(f"{icon} {t['title']}"), 
                callback_data=f"toggle_t_{t['id']}_{subject_id}_{page}"
            ),
            InlineKeyboardButton(
                text=mat_btn_label,
                callback_data=f"view_mat_{t['id']}_{subject_id}_{page}"
            )
        ])
    
    if total is not None:
        controls = _page_controls(f"open_sub_{subject_id}", page, total, page_size)
        if controls:
            kb.append(controls)

    kb.append([
        InlineKeyboardButton(text="➕ Добавить работу", callback_data=f"add_task_prompt_{subject_id}")
    ])
    kb.append([
        InlineKeyboardButton(text="🗑️ Удалить предмет", callback_data=f"confirm_del_sub_{subject_id}"),
        InlineKeyboardButton(text="🔙 К предметам", callback_data="list_subjects")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def materials_list_kb(
    task_id: int,
    subject_id: int,
    materials: List[Dict],
    page: int = 0,
    total: Optional[int] = None,
    page_size: int = 25,
) -> InlineKeyboardMarkup:
    kb = []
    for m in materials:
        icon = "📖" if m["material_type"] == "method" else "💡"
        prefix = "Методичка" if m["material_type"] == "method" else "Решение"
        text = _short_label(f"{icon} {prefix}: {m['title']}")
        kb.append([InlineKeyboardButton(text=text, callback_data=f"dl_mat_{m['id']}_{task_id}_{subject_id}")])

    if total is not None:
        controls = _page_controls(f"view_mat_{task_id}_{subject_id}", page, total, page_size)
        if controls:
            kb.append(controls)
        
    kb.append([
        InlineKeyboardButton(text="📤 Предложить методичку / решение", callback_data=f"upload_mat_{task_id}_{subject_id}")
    ])
    kb.append([
        InlineKeyboardButton(text="🔙 Назад к работам", callback_data=f"open_sub_{subject_id}_{page}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def choose_material_type_kb(task_id: int, subject_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="📖 Методичка (задание / теория)", callback_data=f"set_mat_type_method_{task_id}_{subject_id}"),
            InlineKeyboardButton(text="💡 Решение (готовая работа / шпора)", callback_data=f"set_mat_type_solution_{task_id}_{subject_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_mat_{task_id}_{subject_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_verify_kb(material_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_appr_{material_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rejc_{material_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def bottom_start_kb() -> ReplyKeyboardMarkup:
    """Нижняя постоянная клавиатура с кнопкой '🚀 Начать' для быстрого перезапуска меню."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Начать")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def request_verification_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать — Выбрать группу", callback_data="start_verify_flow")]
    ])

def select_group_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="ТТ-36", callback_data="grp_ТТ-36")],
        [InlineKeyboardButton(text="ТТ-37", callback_data="grp_ТТ-37"), InlineKeyboardButton(text="ТТ-38", callback_data="grp_ТТ-38")],
        [InlineKeyboardButton(text="ТТЧ-37", callback_data="grp_ТТЧ-37"), InlineKeyboardButton(text="ТТЧ-38", callback_data="grp_ТТЧ-38")],
        [InlineKeyboardButton(text="ХК-37", callback_data="grp_ХК-37"), InlineKeyboardButton(text="ХК-38", callback_data="grp_ХК-38"), InlineKeyboardButton(text="ХК-39", callback_data="grp_ХК-39")],
        [InlineKeyboardButton(text="ВД-65", callback_data="grp_ВД-65"), InlineKeyboardButton(text="ВД-66", callback_data="grp_ВД-66"), InlineKeyboardButton(text="ВД-67", callback_data="grp_ВД-67")],
        [InlineKeyboardButton(text="ВД-68", callback_data="grp_ВД-68"), InlineKeyboardButton(text="ВД-69", callback_data="grp_ВД-69"), InlineKeyboardButton(text="ВД-70", callback_data="grp_ВД-70")],
        [InlineKeyboardButton(text="С-37", callback_data="grp_С-37"), InlineKeyboardButton(text="С-38", callback_data="grp_С-38"), InlineKeyboardButton(text="С-39", callback_data="grp_С-39")],
        [InlineKeyboardButton(text="СР-38", callback_data="grp_СР-38")],
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_user_verify_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить доступ", callback_data=f"adm_u_appr_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_u_rejc_{user_id}")
        ]
    ])

def rejected_user_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Выбрать группу заново", callback_data="start_verify_flow")],
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
    ])

def admin_panel_kb(pending_materials_count: int = 0, pending_users_count: int = 0, pending_feedback_count: int = 0) -> InlineKeyboardMarkup:
    kb = []
    if pending_users_count > 0:
        kb.append([
            InlineKeyboardButton(text=f"👥 Заявки на вход ({pending_users_count})", callback_data="adm_users_queue")
        ])
    if pending_materials_count > 0:
        kb.append([
            InlineKeyboardButton(text=f"📋 Заявки на материалы ({pending_materials_count})", callback_data="admin_review_queue")
        ])
    if pending_feedback_count > 0:
        kb.append([
            InlineKeyboardButton(text=f"🐞 Отчёты об ошибках ({pending_feedback_count})", callback_data="adm_feedback_queue")
        ])
    kb.append([
        InlineKeyboardButton(text="🧪 Режим тестирования групп", callback_data="adm_test_mode")
    ])
    kb.append([
        InlineKeyboardButton(text="📅 Опубликовать расписание для всех", callback_data="adm_sched_publish_all")
    ])
    kb.append([
        InlineKeyboardButton(text="📢 Отправить объявление всем", callback_data="adm_broadcast_start")
    ])
    kb.append([
        InlineKeyboardButton(text="👥 Список верифицированных студентов", callback_data="adm_users_list")
    ])
    kb.append([
        InlineKeyboardButton(text="🗄️ База знаний (удалить неактуальное)", callback_data="adm_manage_materials")
    ])
    kb.append([
        InlineKeyboardButton(text="🔄 Обновить статус", callback_data="admin_panel"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_test_groups_kb(groups: List[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=group, callback_data=f"adm_test_preview_{group}")] for group in groups]
    rows.append([InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_test_preview_kb(group_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Посмотреть расписание", callback_data=f"sched_view_{group_name}")],
        [InlineKeyboardButton(text="👥 Выбрать другую группу", callback_data="adm_test_mode")],
        [InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_panel")],
    ])


def admin_feedback_queue_kb(reports: List[Dict], page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows = []
    for report in reports:
        label = _short_label(f"🐞 {report.get('user_label') or report['user_id']}: {report.get('body_text') or 'вложение'}", 48)
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_feedback_{report['id']}")])
    controls = _page_controls("adm_feedback_queue", page, total, page_size)
    if controls:
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_feedback_item_kb(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Открыть исходное сообщение", callback_data=f"adm_feedback_copy_{report_id}")],
        [InlineKeyboardButton(text="✅ Закрыть отчёт", callback_data=f"adm_feedback_resolve_{report_id}")],
        [InlineKeyboardButton(text="🔙 К отчётам", callback_data="adm_feedback_queue")],
    ])


def _page_controls(callback_prefix: str, page: int, total: int, page_size: int) -> List[InlineKeyboardButton]:
    last_page = max(0, (total - 1) // page_size)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{callback_prefix}_{page - 1}"))
    if page < last_page:
        buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"{callback_prefix}_{page + 1}"))
    return buttons


def admin_user_queue_page_kb(users: List[Dict], page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows = []
    for user in users:
        label = _short_label(user.get("student_name") or user.get("username") or str(user["user_id"]), 22)
        rows.append([
            InlineKeyboardButton(text=f"✅ {label}", callback_data=f"adm_u_appr_{user['user_id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"adm_u_rejc_{user['user_id']}"),
        ])
    controls = _page_controls("adm_users_queue", page, total, page_size)
    if controls:
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_material_queue_page_kb(materials: List[Dict], page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows = []
    for material in materials:
        label = _short_label(material.get("title") or f"Материал {material['id']}", 22)
        rows.append([
            InlineKeyboardButton(text=f"✅ {label}", callback_data=f"adm_appr_{material['id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"adm_rejc_{material['id']}"),
        ])
    controls = _page_controls("admin_review_queue", page, total, page_size)
    if controls:
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_verified_users_page_kb(page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows = []
    controls = _page_controls("adm_users_list", page, total, page_size)
    if controls:
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def debt_tasks_page_kb(page: int, total: int, page_size: int = 25) -> InlineKeyboardMarkup:
    rows = []
    controls = _page_controls("show_all_debts", page, total, page_size)
    if controls:
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="🔙 К трекеру долгов", callback_data="debts_hub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm_delete_subject_kb(subject_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="⚠️ Да, удалить со всеми работами", callback_data=f"del_sub_{subject_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"open_sub_{subject_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_to_subject_kb(subject_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к работам", callback_data=f"open_sub_{subject_id}")]
    ])

def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ])

def quick_subjects_select_kb(subjects: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in subjects:
        kb.append([InlineKeyboardButton(text=_short_label(f"📚 {s['name']}"), callback_data=f"quick_sub_{s['id']}")])
    kb.append([
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def quick_tasks_select_kb(
    subject_id: int, tasks: List[Dict], page: int = 0, total: Optional[int] = None, page_size: int = 25
) -> InlineKeyboardMarkup:
    kb = []
    for t in tasks:
        kb.append([InlineKeyboardButton(text=_short_label(f"📝 {t['title']}"), callback_data=f"quick_task_{t['id']}_{subject_id}")])
    if total is not None:
        controls = _page_controls(f"quick_sub_{subject_id}", page, total, page_size)
        if controls:
            kb.append(controls)
    kb.append([
        InlineKeyboardButton(text="➕ Другая / Новая работа", callback_data=f"quick_new_task_{subject_id}")
    ])
    kb.append([
        InlineKeyboardButton(text="🔙 К выбору предмета", callback_data="quick_upload_material")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def material_sent_success_kb(subject_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К работам предмета", callback_data=f"open_sub_{subject_id}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def text_only_or_cancel_kb(task_id: int, subject_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Сохранить только этот текст (без фото)", callback_data=f"save_text_mat_{task_id}_{subject_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
    ])

def materials_hub_kb(subjects_with_counts: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in subjects_with_counts:
        name = s["name"]
        cnt = s.get("count", 0)
        icon = "🟢" if cnt > 0 else "⚪"
        badge = f" ({cnt})" if cnt > 0 else ""
        kb.append([
            InlineKeyboardButton(text=_short_label(f"{icon} {name}{badge}"), callback_data=f"b_sub_{subject_callback_token(name)}")
        ])
    kb.append([
        InlineKeyboardButton(text="📤 Добавить методичку или решение", callback_data="quick_upload_material")
    ])
    kb.append([
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def subject_materials_view_kb(subject_name: str, materials: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for m in materials:
        icon = "📖" if m["material_type"] == "method" else "💡"
        prefix = "Методичка" if m["material_type"] == "method" else "Решение"
        task_label = f" ({m['task_title']})" if m.get("task_title") else ""
        btn_text = f"{icon} {prefix}: {m['title']}{task_label}"
        if len(btn_text) > 42:
            btn_text = btn_text[:39] + "..."
        kb.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"dl_mat_{m['id']}")
        ])
    kb.append([
        InlineKeyboardButton(text="📤 Добавить материал к этому предмету", callback_data=f"quick_sub_name_{subject_callback_token(subject_name)}")
    ])
    kb.append([
        InlineKeyboardButton(text="🔙 К списку предметов", callback_data="browse_materials_hub"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def material_downloaded_kb(subject_name: str, material_id: Optional[int] = None, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    kb = []
    if is_admin_user and material_id:
        kb.append([
            InlineKeyboardButton(text="🗑️ Удалить этот материал (неактуально)", callback_data=f"adm_ask_del_{material_id}")
        ])
    kb.append([
        InlineKeyboardButton(text=_short_label(f"🔙 К материалам «{subject_name}»"), callback_data=f"b_sub_{subject_callback_token(subject_name)}")
    ])
    kb.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_confirm_delete_material_kb(material_id: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="⚠️ Да, удалить навсегда", callback_data=f"adm_do_del_{material_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="adm_manage_materials")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_materials_subjects_kb(subjects_with_counts: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in subjects_with_counts:
        name = s["name"]
        cnt = s.get("count", 0)
        if cnt > 0:
            kb.append([
                InlineKeyboardButton(text=_short_label(f"📁 {name} ({cnt} шт.)"), callback_data=f"adm_sub_mats_{subject_callback_token(name)}")
            ])
    kb.append([
        InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_panel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_subject_materials_delete_kb(subject_name: str, materials: List[Dict]) -> InlineKeyboardMarkup:
    kb = []
    for m in materials:
        icon = "📖" if m["material_type"] == "method" else "💡"
        title = m["title"]
        task_label = f" ({m['task_title']})" if m.get("task_title") else ""
        btn_text = f"🗑️ {icon} {title}{task_label}"
        if len(btn_text) > 42:
            btn_text = btn_text[:39] + "..."
        kb.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"adm_ask_del_{m['id']}")
        ])
    kb.append([
        InlineKeyboardButton(text="🔙 К выбору предмета", callback_data="adm_manage_materials"),
        InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_panel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)
