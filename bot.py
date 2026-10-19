import asyncio
import datetime
import functools
import html
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

import database as db
import keyboards as kb
import gdrive_sync


class AsyncDatabase:
    """Runs the synchronous SQLite API in a small worker pool, keeping Telegram's loop responsive."""

    def __init__(self, module):
        self._module = module
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sqlite")
        self._wrappers = {}

    def __getattr__(self, name):
        value = getattr(self._module, name)
        if not callable(value):
            return value
        if name not in self._wrappers:
            function = value

            async def run(*args, **kwargs):
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    self._executor,
                    functools.partial(function, *args, **kwargs),
                )

            self._wrappers[name] = run
        return self._wrappers[name]


adb = AsyncDatabase(db)

# Настройка логирования: stdout для Docker/systemd, ротируемый файл только для ручного запуска.
log_handlers = [logging.StreamHandler(sys.stdout)]
log_file = os.getenv("BOT_LOG_FILE")
if log_file:
    log_handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    handlers=log_handlers,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Токен бота и супер-администратор
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = "Menmamoh"

def _env_admin_id() -> Optional[int]:
    raw_admin_id = os.getenv("ADMIN_ID", "").strip()
    if not raw_admin_id:
        return None
    try:
        return int(raw_admin_id)
    except ValueError:
        logger.error("ADMIN_ID must be a numeric Telegram user ID")
        return None

ADMIN_ID = _env_admin_id()

def is_admin(user) -> bool:
    """Checks the administrator's stable Telegram numeric ID."""
    return bool(user and ADMIN_ID and user.id == ADMIN_ID)

MAIN_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "helper_cat.png")
SHARK_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "meme_shark.jpg")
GENERAL_SCHEDULE_PHOTOS = (
    (
        os.path.join(os.path.dirname(__file__), "assets", "schedule", "replacement_monday_19-10-2026_page_1.jpeg"),
        "Замены на понедельник, 19.10.2026 — часть 1",
        datetime.date(2026, 10, 19),
    ),
    (
        os.path.join(os.path.dirname(__file__), "assets", "schedule", "replacement_monday_19-10-2026_page_2.jpeg"),
        "Замены на понедельник, 19.10.2026 — часть 2",
        datetime.date(2026, 10, 19),
    ),
    (
        os.path.join(os.path.dirname(__file__), "assets", "schedule", "replacement_saturday_17-10-2026.jpeg"),
        "Замены на субботу, 17.10.2026",
        datetime.date(2026, 10, 17),
    ),
    (
        os.path.join(os.path.dirname(__file__), "assets", "schedule", "consultations_monday_19-10-2026.jpeg"),
        "Факультативы и консультации на понедельник, 19.10.2026",
        datetime.date(2026, 10, 19),
    ),
)


def get_current_general_schedule_photos(target_date: Optional[datetime.date] = None) -> List[Tuple[str, str, datetime.date]]:
    """Возвращает свежие фото из Google Диска либо локальные заготовки строго для целевой даты."""
    if target_date is None:
        target_date, _, _ = gdrive_sync.get_target_study_date()
    gdrive_photos = gdrive_sync.get_active_gdrive_schedule_photos(target_date=target_date)
    if gdrive_photos:
        return gdrive_photos
    return [
        (path, caption, valid_until)
        for path, caption, valid_until in GENERAL_SCHEDULE_PHOTOS
        if valid_until >= target_date
    ]


def get_general_schedule_photo(gen_sched: Optional[Dict], target_date: Optional[datetime.date] = None):
    """Возвращает актуальное фото замен из Google Диска на целевой день (прошедшие дни отсекаются)."""
    if gen_sched and gen_sched.get("photo_file_id"):
        return gen_sched["photo_file_id"]

    if target_date is None:
        target_date, _, _ = gdrive_sync.get_target_study_date()

    photos_pool = get_current_general_schedule_photos(target_date=target_date)
    available_paths = [
        path for path, _, valid_until in photos_pool
        if valid_until >= target_date and os.path.isfile(path)
    ]
    if len(available_paths) >= 2:
        return available_paths
    return available_paths[0] if available_paths else None


def build_general_schedule_media(photo_paths: List[str], target_date: Optional[datetime.date] = None) -> List[InputMediaPhoto]:
    """Готовит исходные фото общего листа для отправки альбомом."""
    photos_pool = get_current_general_schedule_photos(target_date=target_date)
    captions_by_path = {path: caption for path, caption, _ in photos_pool}
    return [
        InputMediaPhoto(
            media=FSInputFile(path),
            caption=captions_by_path.get(path),
            parse_mode="HTML",
        )
        for path in photo_paths
    ]


# Состояния FSM
class Form(StatesGroup):
    waiting_for_subject_name = State()
    waiting_for_task_title = State()
    waiting_for_quick_task_title = State()
    waiting_for_material_title = State()
    waiting_for_material_file = State()
    waiting_for_schedule_pair = State()
    waiting_for_group_schedule = State()
    waiting_for_custom_group = State()
    waiting_for_student_name = State()
    waiting_for_feedback = State()
    waiting_for_broadcast = State()

async def check_user_access(user) -> tuple[bool, str]:
    if is_admin(user):
        return True, ""
    user_rec = await adb.get_user(user.id)
    if not user_rec:
        return False, "🔒 Требуется выбрать свою группу!"
    if user_rec.get("status") == "pending":
        return False, "⏳ Твоя заявка на рассмотрении у администрации!"
    if user_rec.get("status") == "rejected":
        return False, "⛔ Твой доступ в бота был отклонён администрацией!"
    if user_rec.get("status") != "approved":
        return False, "🔒 Доступ пока не подтверждён администрацией."
    return True, ""

async def safe_edit_message(
    target, 
    text: str, 
    reply_markup: Optional[InlineKeyboardMarkup] = None, 
    parse_mode: str = "HTML"
):
    """Безопасно редактирует сообщение (текст или подпись к фото) без зависания кнопок."""
    cb = target if (isinstance(target, CallbackQuery) or hasattr(target, "message")) else None
    msg = getattr(cb, "message", None) if cb else target
    bot_inst = getattr(target, "bot", None) or getattr(msg, "bot", None)
    user_id = target.from_user.id if hasattr(target, "from_user") and target.from_user else (msg.chat.id if hasattr(msg, "chat") and msg.chat else 0)

    if cb and hasattr(cb, "answer"):
        try:
            await cb.answer()
        except Exception:
            pass

    # Если исходное сообщение содержало фото — редактируем подпись к нему
    if getattr(msg, "photo", None) and hasattr(msg, "edit_caption"):
        try:
            await msg.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            if cb and getattr(msg.chat, "type", None) == "private":
                await adb.set_ui_message_id(cb.from_user.id, msg.message_id)
            return
        except Exception:
            pass

    # Если текстовое сообщение или edit_caption не сработал — пробуем edit_text
    if hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            if cb and getattr(msg.chat, "type", None) == "private":
                await adb.set_ui_message_id(cb.from_user.id, msg.message_id)
            return
        except Exception:
            pass

    # Если редактирование невозможно, удаляем старое и отправляем новое
    if hasattr(msg, "delete"):
        try:
            await msg.delete()
        except Exception:
            pass

    if bot_inst and hasattr(bot_inst, "send_message") and user_id:
        try:
            sent = await bot_inst.send_message(chat_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            await adb.set_ui_message_id(user_id, sent.message_id)
            return
        except Exception as e:
            logger.warning(f"Could not send message in safe_edit_message for user {user_id}: {e}")


async def show_user_screen(
    target,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    photo=None,
):
    """Edits the user's one persistent private-chat interface message when possible."""
    callback = target if isinstance(target, CallbackQuery) else None
    message = callback.message if callback else target
    user = callback.from_user if callback else getattr(target, "from_user", None)
    if not user or not message or getattr(message.chat, "type", None) != "private":
        if callback:
            await safe_edit_message(callback, text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return

    user_id = user.id
    if callback:
        try:
            await callback.answer()
        except Exception:
            pass
        try:
            if getattr(message, "photo", None):
                await message.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            else:
                await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            await adb.set_ui_message_id(user_id, message.message_id)
            return
        except Exception as error:
            if "message is not modified" in str(error).lower():
                await adb.set_ui_message_id(user_id, message.message_id)
                return
        try:
            await message.delete()
        except Exception:
            pass
        sent = await callback.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        await adb.set_ui_message_id(user_id, sent.message_id)
        return

    bot_inst = target.bot
    saved_id = await adb.get_ui_message_id(user_id)
    # A message or command from the user starts a fresh screen. Send it first,
    # then remove the previous bot menu so it never remains above the new one.
    if photo:
        sent = await target.answer_photo(
            photo=photo,
            caption=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    else:
        sent = await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    await adb.set_ui_message_id(user_id, sent.message_id)
    if saved_id and saved_id != sent.message_id:
        try:
            await bot_inst.delete_message(chat_id=message.chat.id, message_id=saved_id)
        except Exception as error:
            logger.debug("Could not remove previous UI message %s for user %s: %s", saved_id, user_id, error)


async def render_admin_verification_queue(bot: Bot, page: int = 0):
    """Creates or refreshes one dedicated admin message for verification requests."""
    admin_id = await adb.get_admin_id() or ADMIN_ID
    if not admin_id:
        return

    page = max(0, page)
    total = await adb.count_pending_users()
    pending_users = await adb.get_pending_users(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
    if not pending_users and page > 0:
        page = max(0, (total - 1) // ADMIN_PAGE_SIZE)
        pending_users = await adb.get_pending_users(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)

    if pending_users:
        lines = [f"👥 <b>Очередь верификации: {total}</b> · страница {page + 1}", ""]
        for index, user in enumerate(pending_users, page * ADMIN_PAGE_SIZE + 1):
            name = html.escape(user.get("student_name") or user.get("tg_name") or "Студент")
            group = html.escape(user.get("student_group") or "")
            username = html.escape(user.get("username") or f"ID_{user['user_id']}")
            lines.append(f"{index}. <b>{name}</b> · {group} · {username}")
        text = "\n".join(lines)
        markup = kb.admin_user_queue_page_kb(pending_users, page, total, ADMIN_PAGE_SIZE)
    else:
        text = "👥 <b>Очередь верификации пуста.</b>\nНовые заявки появятся здесь."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить очередь", callback_data="adm_users_queue")],
            [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")],
        ])

    saved_id = await adb.get_verification_ui_message_id(admin_id)
    if saved_id:
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=saved_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception as error:
            if "message is not modified" in str(error).lower():
                return
            await adb.clear_verification_ui_message_id(admin_id)

    try:
        sent = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
        await adb.set_verification_ui_message_id(admin_id, sent.message_id)
    except Exception:
        logger.exception("Could not show verification queue to admin %s", admin_id)

async def cleanup_legacy_user_menus(bot: Bot):
    """Deletes last tracked legacy menu per user without touching verification data."""
    if not await adb.claim_initial_ui_cleanup():
        return

    messages = await adb.get_all_ui_messages()
    deleted = 0
    failed = 0
    for entry in messages:
        user_id = entry["user_id"]
        message_id = entry["message_id"]
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
            await adb.clear_ui_message_id(user_id)
            deleted += 1
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.delete_message(chat_id=user_id, message_id=message_id)
                await adb.clear_ui_message_id(user_id)
                deleted += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    logger.info("Legacy menu cleanup finished: deleted=%s, unavailable=%s", deleted, failed)


async def prompt_verification_required(target, user, err_reason: str = ""):
    """Отправляет экран с кнопкой выбора группы тем, кто ещё не подтверждён"""
    user_rec = await adb.get_user(user.id)
    if user_rec and user_rec.get("status") == "pending":
        s_grp = html.escape(user_rec.get("student_group") or "")
        text = (
            f"⏳ <b>Твоя заявка находится на рассмотрении!</b> 🎓\n\n"
            f"🎓 <b>Выбранная группа:</b> {s_grp}\n\n"
            f"Заявка ожидает подтверждения администратора.\n"
            f"Как только доступ одобрят, ты получишь сообщение и сможешь пользоваться ботом!"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
    elif user_rec and user_rec.get("status") == "rejected":
        text = (
            f"⛔ <b>Твой доступ в «Помощник Пятнашки» был отклонён администрацией.</b>\n\n"
            f"Если произошла ошибка или ты ошибся группой, нажми кнопку ниже, чтобы отправить заявку повторно 👇"
        )
        markup = kb.rejected_user_kb()
    else:
        text = (
            f"🔒 <b>Вход в «Помощник Пятнашки»</b> 🎓\n\n"
            f"В боте действует система доступа по учебным группам колледжа им. А.Е. Ларина.\n\n"
            f"Выбери свою группу ниже, чтобы получить доступ к расписанию и учебным материалам 👇"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Выбрать группу для доступа", callback_data="start_verify_flow")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
    if isinstance(target, CallbackQuery):
        await safe_edit_message(target, text, reply_markup=markup)
    else:
        await show_user_screen(target, text, reply_markup=markup)


async def get_user_primary_group(user_id: int) -> Optional[str]:
    """Определяет основную группу пользователя (None, если группа ещё не выбрана)"""
    user_rec = await adb.get_user(user_id)
    if user_rec and user_rec.get("student_group"):
        return user_rec["student_group"]
    return None

async def user_has_study_features(user, user_record: Optional[Dict] = None) -> bool:
    """Проверяет, открыт ли для группы пользователя трекер долгов и база методичек."""
    if is_admin(user):
        return True
    if not user:
        return False
    if user_record is None:
        user_record = await adb.get_user(user.id)
    grp = user_record.get("student_group") if user_record else None
    return await adb.group_has_subjects_support(grp)

async def is_tt36_student(user) -> bool:
    """Алиас для проверки доступа к учебным разделам."""
    return await user_has_study_features(user)

async def reject_non_tt36(callback: CallbackQuery):
    """Показывает предупреждение для студентов других групп о том, что предметы/методички пока не настроены"""
    grp = await get_user_primary_group(callback.from_user.id) or "не указана"
    text = (
        f"ℹ️ <b>Раздел предметов и методичек</b>\n\n"
        f"Учебные предметы, работы и методички в данный момент настроены для групп <b>ТТ-36</b>, <b>ХК</b> и <b>ВД</b>.\n\n"
        f"Для твоей группы (<code>{html.escape(grp)}</code>) доступно актуальное расписание пар и звонков колледжа! 📅"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Открыть расписание", callback_data="schedule_menu")],
        [InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="sched_bells")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    msg = getattr(callback, "message", None)
    if msg and hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
    elif hasattr(callback, "bot") and hasattr(callback.bot, "send_message"):
        try:
            await callback.bot.send_message(chat_id=callback.from_user.id, text=text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
    if hasattr(callback, "answer"):
        try:
            await callback.answer("Раздел пока недоступен для твоей группы", show_alert=True)
        except Exception:
            pass


dp = Dispatcher(storage=MemoryStorage())

class VerificationMiddleware(BaseMiddleware):
    """Blocks unverified users at the update boundary, including stale buttons and FSM input."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if isinstance(event, CallbackQuery) and event.message and event.message.chat.type != "private":
            callback_data = event.data or ""
            private_prefixes = (
                "open_sub_", "toggle_t_", "view_mat_", "dl_mat_", "add_task_prompt_",
                "confirm_del_sub_", "del_sub_", "b_sub_", "quick_", "upload_mat_",
                "set_mat_type_", "save_text_mat_", "adm_",
            )
            private_callbacks = {
                "main_menu", "debts_hub", "show_all_debts", "list_subjects",
                "add_subject_prompt", "browse_materials_hub", "start_verify_flow", "feedback_start",
            }
            if callback_data in private_callbacks or callback_data.startswith(private_prefixes):
                await event.answer("Открой личный чат с ботом для личных разделов.", show_alert=True)
                return None
        if not user or is_admin(user) or await adb.is_user_verified(user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if callback_data in {"main_menu", "start_verify_flow", "feedback_start"} or callback_data.startswith("grp_"):
                return await handler(event, data)
        elif isinstance(event, Message):
            if event.text:
                command = event.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
                clean = event.text.strip().lower()
                if command in {"/start", "/verify", "/registration", "/group", "/my_id", "/id"} or clean in {"начать", "🚀 начать", "старт", "start", "меню"}:
                    return await handler(event, data)
            state = data.get("state")
            if state and await state.get_state() in {
                Form.waiting_for_custom_group.state,
                Form.waiting_for_feedback.state,
            }:
                return await handler(event, data)

        await prompt_verification_required(event, user)
        return None


verification_middleware = VerificationMiddleware()
dp.message.middleware(verification_middleware)
dp.callback_query.middleware(verification_middleware)

# --- Команда /my_id ---
@dp.message(Command("my_id", "id"))
async def cmd_my_id(message: Message):
    user_id = message.from_user.id
    is_adm = " (администратор 👑)" if is_admin(message.from_user) else ""
    await show_user_screen(message,
        f"🆔 Твой Telegram ID: <code>{user_id}</code>\n"
        f"👤 Твой Username: @{html.escape(message.from_user.username or 'отсутствует')}{is_adm}",
        reply_markup=kb.back_to_menu_kb()
    )

# --- Панель администратора (@Menmamoh) ---
ADMIN_PAGE_SIZE = 10

async def show_admin_panel(message_or_callback, user, bot: Bot, is_callback: bool = False):
    if not is_admin(user):
        text = (
            "🔒 <b>Доступ запрещен.</b>\n"
            "Панель управления доступна только администратору."
        )
        if is_callback:
            await message_or_callback.message.edit_text(text, parse_mode="HTML")
        else:
            await message_or_callback.answer(text, parse_mode="HTML")
        return
        
    global ADMIN_ID
    ADMIN_ID = user.id
    await adb.set_admin_id(user.id)
    await adb.ensure_admin_user(user.id, user.username or ADMIN_USERNAME)
    
    pending_mat_count, pending_users_count = await asyncio.gather(
        adb.count_pending_materials(),
        adb.count_pending_users(),
    )
    
    markup = kb.admin_panel_kb(pending_materials_count=pending_mat_count, pending_users_count=pending_users_count)
    
    status_lines = []
    if pending_users_count > 0:
        status_lines.append(f"👥 <b>Заявок на вход (верификация):</b> <b>{pending_users_count}</b> шт.")
    if pending_mat_count > 0:
        status_lines.append(f"📋 <b>Материалов на проверке:</b> <b>{pending_mat_count}</b> шт.")
        
    if not status_lines:
        text = (
            f"👑 <b>Панель администратора (@{ADMIN_USERNAME})</b>\n\n"
            f"🎉 <b>Все заявки проверены!</b> Очередь пуста.\n\n"
            f"Здесь ты можешь управлять доступом студентов, расписанием и базой знаний."
        )
    else:
        text = (
            f"👑 <b>Панель администратора (@{ADMIN_USERNAME})</b>\n\n"
            + "\n".join(status_lines) + "\n\n"
            f"Выбери раздел для управления 👇"
        )
    await show_user_screen(message_or_callback, text, reply_markup=markup)
        
@dp.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot):
    if is_admin(message.from_user) and message.chat.type != "private":
        await message.answer("Админ-панель доступна только в личном чате с ботом.")
        return
    await show_admin_panel(message, message.from_user, bot, is_callback=False)

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery, bot: Bot):
    if is_admin(callback.from_user) and callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом, чтобы пользоваться админ-панелью.", show_alert=True)
        return
    await show_admin_panel(callback, callback.from_user, bot, is_callback=True)
    await callback.answer()


@dp.callback_query(F.data == "adm_broadcast_start")
async def cb_admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ только для администратора.", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для отправки объявления.", show_alert=True)
        return
    await state.set_state(Form.waiting_for_broadcast)
    text = (
        "📢 <b>Новое объявление</b>\n\n"
        "Отправь текст или фотографию с подписью. Перед рассылкой покажу предпросмотр и попрошу подтвердить отправку.\n\n"
        "Объявление получат все подтверждённые пользователи бота. Для отмены нажми «Главное меню»."
    )
    await show_user_screen(callback, text, reply_markup=kb.back_to_menu_kb())


@dp.message(Form.waiting_for_broadcast)
async def process_broadcast_draft(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        await state.clear()
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
    if not (message.text or message.photo):
        await show_user_screen(message, "⚠️ Отправь текст объявления или фотографию с подписью.", reply_markup=kb.back_to_menu_kb())
        return

    await state.update_data(
        announcement_chat_id=message.chat.id,
        announcement_message_id=message.message_id,
        announcement_is_photo=bool(message.photo),
        announcement_text=message.caption or message.text or "",
    )
    safe_text = html.escape(message.caption or message.text or "")
    preview = "📢 <b>Предпросмотр объявления</b>\n\n"
    if message.photo:
        preview += "🖼️ <i>К объявлению приложена фотография.</i>\n"
    preview += safe_text or "<i>Без текста</i>"
    preview += "\n\nОтправить всем подтверждённым пользователям?"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить всем", callback_data="adm_broadcast_send")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="adm_broadcast_cancel")],
    ])
    await show_user_screen(message, preview, reply_markup=markup)


@dp.callback_query(F.data == "adm_broadcast_cancel")
async def cb_admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ только для администратора.", show_alert=True)
        return
    await state.clear()
    await show_admin_panel(callback, callback.from_user, bot, is_callback=True)
    await callback.answer("Рассылка отменена.")


@dp.callback_query(F.data == "adm_broadcast_send")
async def cb_admin_broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ только для администратора.", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом.", show_alert=True)
        return
    draft = await state.get_data()
    source_chat_id = draft.get("announcement_chat_id")
    source_message_id = draft.get("announcement_message_id")
    if not source_chat_id or not source_message_id:
        await state.clear()
        await callback.answer("Черновик не найден. Создай объявление заново.", show_alert=True)
        await show_admin_panel(callback, callback.from_user, bot, is_callback=True)
        return

    await state.clear()
    await callback.message.edit_text("📢 <b>Отправляю объявление…</b>", parse_mode="HTML", reply_markup=None)
    sent_count = 0
    failed_count = 0
    users = await adb.get_verified_users()
    for recipient in users:
        user_id = recipient["user_id"]
        if user_id == callback.from_user.id:
            continue
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent_count += 1
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=source_chat_id, message_id=source_message_id)
                sent_count += 1
            except Exception:
                failed_count += 1
                logger.exception("Could not deliver announcement to user %s", user_id)
        except Exception:
            failed_count += 1
            logger.exception("Could not deliver announcement to user %s", user_id)
        await asyncio.sleep(0.05)

    report = (
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Доставлено: <b>{sent_count}</b>\n"
        f"Не удалось отправить: <b>{failed_count}</b>"
    )
    await callback.message.edit_text(
        report,
        parse_mode="HTML",
        reply_markup=kb.admin_panel_kb(await adb.count_pending_materials(), await adb.count_pending_users()),
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_review_queue")
@dp.callback_query(F.data.startswith("admin_review_queue_"))
async def cb_admin_review_queue(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ только для администратора.", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом, чтобы пользоваться админ-панелью.", show_alert=True)
        return
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "admin_review_queue" else 0
    total = await adb.count_pending_materials()
    materials = await adb.get_pending_materials(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
    if not materials:
        text = "👑 <b>Панель администратора</b>\n\n📋 Очередь материалов пуста."
        markup = kb.admin_panel_kb(await adb.count_pending_materials(), await adb.count_pending_users())
    else:
        lines = [f"📋 <b>Материалы на проверке: {total}</b> · страница {page + 1}", ""]
        for index, material in enumerate(materials, page * ADMIN_PAGE_SIZE + 1):
            title = html.escape((material.get("title") or "Без названия")[:90])
            subject = html.escape((material.get("subject_name") or "")[:40])
            task = html.escape((material.get("task_title") or "")[:60])
            lines.append(f"{index}. <b>{title}</b> — {subject} / {task}")
        text = "\n".join(lines)
        markup = kb.admin_material_queue_page_kb(materials, page, total, ADMIN_PAGE_SIZE)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer("Очередь загружена!")

# --- Команда /start ---
@dp.message(CommandStart())
@dp.message(F.text.lower().in_({"начать", "🚀 начать", "старт", "start", "/start", "меню", "главное меню"}))
async def cmd_start(message: Message, state: FSMContext):
    global ADMIN_ID
    await state.clear()
    user_id = message.from_user.id
    first_name = html.escape(message.from_user.first_name or "студент")

    if message.chat.type != "private":
        await message.answer("Личные разделы доступны только в личном чате с ботом. Открой бота напрямую и отправь /start.")
        return
    
    # Автоматически регистрируем ID админа @Menmamoh при первом контакте с ботом
    if is_admin(message.from_user):
        ADMIN_ID = user_id
        await adb.set_admin_id(user_id)
        await adb.ensure_admin_user(user_id, message.from_user.username or ADMIN_USERNAME)
        logger.info(f"Authorized primary ADMIN @{ADMIN_USERNAME} (ID: {user_id})")
    else:
        logger.info(f"User {user_id} ({first_name}, @{message.from_user.username}) triggered /start")
        
        # Проверяем статус верификации студента
        user_record = await adb.get_user(user_id)
        if not user_record:
            welcome_text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓\n"
                f"<i>УО «БГК им. А.Е. Ларина» (Пятнашка)</i>\n\n"
                f"«Вообще-то, вход в колледж только по студенческим и группам!» 🐾\n\n"
                f"🔒 <b>Выбери свою учебную группу:</b>\n"
                f"Нажми на группу из списка колледжа ниже, чтобы получить доступ к расписанию и шпорам 👇"
            )
            await show_user_screen(
                message,
                welcome_text,
                reply_markup=kb.select_group_kb(),
            )
            return
            
        status = user_record.get("status")
        if status == "pending":
            s_grp = html.escape(user_record.get("student_group") or "")
            text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓\n"
                f"<i>УО «БГК им. А.Е. Ларина» (Пятнашка)</i>\n\n"
                f"⏳ <b>Твоя заявка находится на рассмотрении!</b>\n\n"
                f"🎓 <b>Выбранная группа:</b> <code>{s_grp}</code>\n\n"
                f"«Вообще-то, администрация уже проверяет списки группы. "
                f"Как только доступ подтвердят — тебе сразу прилетит оповещение!» 🔔"
            )
            pending_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
            ])
            await show_user_screen(
                message,
                text,
                reply_markup=pending_kb,
            )
            return
            
        if status == "rejected":
            text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓\n\n"
                f"⛔ <b>Твой доступ в бота был отклонён администрацией.</b>\n\n"
                f"Если произошла ошибка или ты ошибся группой, подай заявку повторно 👇"
            )
            await show_user_screen(message, text, reply_markup=kb.rejected_user_kb())
            return
            
    # Проверяем группу и статус пользователя
    is_adm = is_admin(message.from_user)
    user_record = await adb.get_user(user_id)
    is_verified = is_adm or bool(user_record and user_record.get("status") == "approved")
    verification_pending = bool(user_record and user_record.get("status") == "pending")
    student_group = user_record.get("student_group") if user_record else None
    pending_count = (await adb.count_pending_materials() + await adb.count_pending_users()) if is_adm else 0
    has_study = await user_has_study_features(message.from_user, user_record)

    # Автоматически загружаем предметы семестра для поддерживаемых групп.
    if has_study:
        await adb.seed_default_subjects(user_id, student_group=student_group)
    
    safe_group = html.escape(student_group or "")
    group_str = f" [<code>{safe_group}</code>]" if student_group else ""
    if has_study:
        summary = await adb.get_all_debts_summary(user_id)
        text = (
            f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓{group_str}\n"
            f"<i>УО «БГК им. А.Е. Ларина»</i>\n\n"
            f"«Вообще-то, пары сами себя не отсидят, а лабы и курсовые сами себя не закроют!» 🐾\n\n"
            f"📅 <b>Расписание:</b> свежие пары и замены колледжа\n"
            f"📝 <b>Трекер долгов:</b> держи хвосты под контролем\n"
            f"📖 <b>База решений:</b> методички, готовые работы и шпоры\n"
            f"🔔 <b>Звонки:</b> точное время пар и перемен\n\n"
            f"📊 <b>Твоя зачётка:</b>\n"
            f"• Активных долгов: <b>{summary['pending']}</b> из {summary['total']}\n"
            f"• Прогресс сдачи: <code>[{summary['bar']}]</code> <b>{summary['percent']}%</b>\n\n"
            f"Куда направляемся? Выбирай раздел в меню ниже 👇"
        )
    else:
        text = (
            f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓{group_str}\n"
            f"<i>УО «БГК им. А.Е. Ларина»</i>\n\n"
            f"«Вообще-то, пары сами себя не отсидят!» 🐾\n\n"
            f"📅 <b>Расписание:</b> актуальные пары и замены для группы <b>{safe_group}</b>\n"
            f"🔔 <b>Звонки:</b> точное время уроков и перемен колледжа\n\n"
            f"💡 <i>(Предметы и методички сейчас настроены для групп ТТ-36, ХК и ВД)</i>\n\n"
            f"Куда направляемся? Выбирай раздел в меню ниже 👇"
        )
    markup = kb.main_menu_kb(
        is_admin_user=is_adm, 
        pending_count=pending_count,
        is_verified=is_verified,
        student_group=student_group,
        is_private_chat=message.chat.type == "private"
    )
    
    await show_user_screen(message, text, reply_markup=markup)

# --- Команда /verify ---
@dp.message(Command("verify", "registration", "group"))
async def cmd_verify(message: Message, state: FSMContext):
    await state.clear()
    if message.chat.type != "private":
        await message.answer("Для регистрации открой личный чат с ботом и отправь /verify.")
        return
    existing = await adb.get_user(message.from_user.id)
    if existing and existing.get("status") in {"approved", "pending"}:
        await cmd_start(message, state)
        return
    text = (
        "🎓 <b>Верификация в «Помощнике Пятнашки»:</b>\n\n"
        "Выбери свою учебную группу из списка колледжа им. А.Е. Ларина или нажми «Другая группа» 👇"
    )
    await show_user_screen(message, text, reply_markup=kb.select_group_kb())

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)


@dp.callback_query(F.data == "feedback_start")
async def cb_feedback_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_feedback)
    text = (
        "🐞 <b>Сообщить об ошибке</b>\n\n"
        "Опиши, что пошло не так и что ты ожидал увидеть. Можно приложить скриншот.\n\n"
        "Отправь сообщение сюда — его получит администратор. Для отмены нажми «Главное меню»."
    )
    await show_user_screen(callback, text, reply_markup=kb.back_to_menu_kb())


@dp.message(Form.waiting_for_feedback)
async def process_feedback(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
    if not (message.text or message.caption or message.photo or message.document):
        await show_user_screen(message, "⚠️ Пришли текст ошибки или приложи скриншот.", reply_markup=kb.back_to_menu_kb())
        return

    admin_id = await adb.get_admin_id() or ADMIN_ID
    if not admin_id:
        await state.clear()
        await show_user_screen(message, "⚠️ Не удалось отправить сообщение: администратор не настроен.", reply_markup=kb.back_to_menu_kb())
        return

    user = message.from_user
    user_label = f"@{user.username}" if user.username else (user.full_name or "Студент")
    header = (
        "🐞 <b>Сообщение об ошибке</b>\n"
        f"👤 {html.escape(user_label)} · ID <code>{user.id}</code>"
    )
    try:
        await bot.send_message(chat_id=admin_id, text=header, parse_mode="HTML")
        await message.copy_to(chat_id=admin_id)
    except Exception:
        logger.exception("Could not forward feedback from user %s", user.id)
        await show_user_screen(message, "⚠️ Не удалось отправить сообщение. Попробуй ещё раз чуть позже.", reply_markup=kb.back_to_menu_kb())
        return

    await state.clear()
    await show_user_screen(
        message,
        "✅ Спасибо! Сообщение об ошибке отправлено администратору.",
        reply_markup=kb.back_to_menu_kb(),
    )

# --- Главное меню ---
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message.chat.type != "private":
        await callback.answer("Личное меню доступно только в личном чате с ботом.", show_alert=True)
        return
    user_id = callback.from_user.id
    is_adm = is_admin(callback.from_user)
    user_record = await adb.get_user(user_id)
    is_verified = is_adm or bool(user_record and user_record.get("status") == "approved")
    verification_pending = bool(user_record and user_record.get("status") == "pending")
    student_group = user_record.get("student_group") if user_record else None
    pending_count = (await adb.count_pending_materials() + await adb.count_pending_users()) if is_adm else 0
    
    markup = kb.main_menu_kb(
        is_admin_user=is_adm,
        pending_count=pending_count,
        is_verified=is_verified,
        student_group=student_group,
        is_private_chat=callback.message.chat.type == "private",
        verification_pending=verification_pending,
    )
    
    group_str = f" [<code>{student_group}</code>]" if student_group else ""
    has_study = await user_has_study_features(callback.from_user, user_record)
    if is_verified:
        if has_study:
            summary = await adb.get_all_debts_summary(user_id)
            grp_label = f" ({student_group})" if student_group else ""
            text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓{group_str}\n"
                f"<i>УО «БГК им. А.Е. Ларина»</i>\n\n"
                f"«Вообще-то, пора закрывать долги и проверять расписание!» 🐾\n\n"
                f"📊 <b>Твои долги{grp_label}:</b> <b>{summary['total'] - summary['pending']}</b> из {summary['total']} сдано (<code>{summary['percent']}%</code>)\n\n"
                f"Выбери нужный раздел:"
            )
        else:
            text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓{group_str}\n"
                f"<i>УО «БГК им. А.Е. Ларина»</i>\n\n"
                f"«Вообще-то, расписание само себя не проверит!» 🐾\n\n"
                f"📅 <b>Расписание:</b> актуальные пары и замены для группы <b>{student_group}</b>\n"
                f"🔔 <b>Звонки:</b> точное время уроков и перемен колледжа\n\n"
                f"💡 <i>(Предметы и методички сейчас настроены для групп ТТ-36, ХК и ВД)</i>\n\n"
                f"Выбери нужный раздел:"
            )
    else:
        if verification_pending:
            text = (
                "⏳ <b>Твоя заявка на проверке</b>\n\n"
                f"🎓 Группа: <code>{html.escape(student_group or '')}</code>\n\n"
                "Дождись решения администратора. Повторно проходить верификацию не нужно."
            )
        else:
            text = (
                f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓\n\n"
                f"⚠️ <b>Твой аккаунт ещё не подтверждён!</b>\n\n"
                f"Нажми кнопку <b>«🔐 Выбрать группу для доступа»</b> ниже, "
                f"чтобы получить доступ к расписанию и учебным материалам 👇"
            )

    await show_user_screen(callback, text, reply_markup=markup)

# =========================================================================
# --- ВЕРИФИКАЦИЯ СТУДЕНТОВ (ОБЯЗАТЕЛЬНЫЙ ВХОД) ---
# =========================================================================

@dp.callback_query(F.data == "start_verify_flow")
async def cb_start_verify_flow(callback: CallbackQuery, state: FSMContext):
    existing = await adb.get_user(callback.from_user.id)
    if existing and existing.get("status") == "approved":
        await callback.answer("Верификация уже пройдена. Выбирай группу в разделе расписания.", show_alert=True)
        return
    if existing and existing.get("status") == "pending":
        text = (
            "⏳ <b>Заявка уже на проверке</b>\n\n"
            f"🎓 Группа: <code>{html.escape(existing.get('student_group') or '')}</code>\n\n"
            "Дождись решения администратора. Повторная заявка не нужна."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ])
        await show_user_screen(callback, text, reply_markup=markup)
        return
    await state.clear()
    text = (
        "🎓 <b>Выбери свою учебную группу:</b>\n\n"
        "Выбери группу из списка колледжа им. Ларина или нажми «Ввести другую группу» 👇"
    )
    await safe_edit_message(callback, text, reply_markup=kb.select_group_kb())

@dp.callback_query(F.data.startswith("grp_"))
async def cb_select_group(callback: CallbackQuery, state: FSMContext):
    existing = await adb.get_user(callback.from_user.id)
    if existing and existing.get("status") == "approved":
        await callback.answer("Верификация уже пройдена. Выбирай группу в разделе расписания.", show_alert=True)
        return
    if existing and existing.get("status") == "pending":
        await callback.answer("Заявка уже на проверке. Повторно выбирать группу не нужно.", show_alert=True)
        return
    data = callback.data[len("grp_"):]
    if data == "custom":
        await state.set_state(Form.waiting_for_custom_group)
        text = (
            "✍️ <b>Введи название своей группы:</b>\n\n"
            "Например: <code>ТТ-36</code> или <code>ХК-37</code>.\n\n"
            "<i>(или отправь /cancel для отмены)</i>"
        )
        await safe_edit_message(callback, text, reply_markup=None)
        return

    if data not in db.COLLEGE_GROUPS:
        try:
            await callback.answer("Такой группы нет в списке. Попробуй выбрать её заново.", show_alert=True)
        except Exception:
            pass
        return
        
    user_id = callback.from_user.id
    tg_uname = callback.from_user.username
    tg_name = callback.from_user.full_name or "Студент"
    student_group = data
    
    # Сохраняем заявку на верификацию без запроса личных имён
    request_created = await adb.add_user_request(
        user_id=user_id,
        username=tg_uname,
        tg_name=tg_name,
        student_name=tg_name,
        student_group=student_group
    )
    if not request_created:
        await callback.answer("Заявка уже создана или доступ уже подтверждён.", show_alert=True)
        return
    await state.clear()
    
    user_confirm_text = (
        f"✅ <b>Заявка на верификацию отправлена!</b>\n\n"
        f"🎓 <b>Выбранная группа:</b> <code>{html.escape(student_group)}</code>\n\n"
        f"Администратор проверит твою заявку. "
        f"Как только доступ подтвердят, ты сразу получишь оповещение в этом чате! 🔔"
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await safe_edit_message(callback, user_confirm_text, reply_markup=confirm_kb)
    
    await render_admin_verification_queue(callback.bot)

@dp.message(Form.waiting_for_custom_group)
async def process_custom_group(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    grp = (message.text or "").strip()
    if not grp:
        await show_user_screen(message, "⚠️ Название группы не может быть пустым. Попробуй ещё раз:", reply_markup=kb.back_to_menu_kb())
        return
    if len(grp.encode("utf-8")) > 40:
        await show_user_screen(message, "⚠️ Название группы слишком длинное. Введи не более 40 байт UTF-8:", reply_markup=kb.back_to_menu_kb())
        return
        
    user_id = message.from_user.id
    tg_uname = message.from_user.username
    tg_name = message.from_user.full_name or "Студент"
    student_group = grp
    
    request_created = await adb.add_user_request(
        user_id=user_id,
        username=tg_uname,
        tg_name=tg_name,
        student_name=tg_name,
        student_group=student_group
    )
    if not request_created:
        await state.clear()
        await cmd_start(message, state)
        return
    await state.clear()
    
    user_confirm_text = (
        f"✅ <b>Заявка на верификацию отправлена!</b>\n\n"
        f"🎓 <b>Выбранная группа:</b> <code>{html.escape(student_group)}</code>\n\n"
        f"Администратор проверит твою заявку. "
        f"Как только доступ подтвердят, ты сразу получишь оповещение в этом чате! 🔔"
    )
    await show_user_screen(message, user_confirm_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ]))
    await render_admin_verification_queue(message.bot)

@dp.message(Form.waiting_for_student_name)
async def process_student_name_legacy(message: Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)

@dp.callback_query(F.data.startswith("adm_u_appr_"))
async def cb_admin_approve_user(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для администратора!", show_alert=True)
        return
        
    target_id = int(callback.data.replace("adm_u_appr_", ""))
    user_record = await adb.get_user(target_id)
    if not user_record:
        await callback.answer("Пользователь не найден!", show_alert=True)
        return
        
    await adb.set_user_status(target_id, "approved")
    await render_admin_verification_queue(bot)
    s_grp = html.escape(user_record.get("student_group") or "")
    u_display = html.escape(user_record.get("student_name") or user_record.get("tg_name") or "Пользователь")
    
    await callback.answer("Одобрено!")
    
    try:
        feature_str = (
            "Тебе открыт доступ к расписанию пар, трекеру долгов и базе знаний."
            if await adb.group_has_subjects_support(s_grp)
            else "Тебе открыт доступ к актуальному расписанию пар и звонков колледжа."
        )
        student_msg = (
            f"🎉 <b>Доступ подтверждён!</b>\n\n"
            f"Твоя верификация для группы <b>{s_grp}</b> успешно одобрена! 🎓\n\n"
            f"{feature_str}\n"
            f"Нажми кнопку ниже, чтобы войти в меню 👇"
        )
        start_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Войти в «Помощник Пятнашки»", callback_data="main_menu")]
        ])
        await bot.send_message(chat_id=target_id, text=student_msg, parse_mode="HTML", reply_markup=start_kb)
    except Exception as e:
        logger.warning(f"Could not notify approved user {target_id}: {e}")

@dp.callback_query(F.data.startswith("adm_u_rejc_"))
async def cb_admin_reject_user(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для администратора!", show_alert=True)
        return
        
    target_id = int(callback.data.replace("adm_u_rejc_", ""))
    user_record = await adb.get_user(target_id)
    if not user_record:
        await callback.answer("Пользователь уже не найден.", show_alert=True)
        return
    await adb.set_user_status(target_id, "rejected")
    await render_admin_verification_queue(bot)
    
    u_display = html.escape(user_record.get("student_name") or user_record.get("tg_name") or "Пользователь") if user_record else "Пользователь"
    await callback.answer("Отклонено!")
    
    try:
        student_msg = (
            f"⛔ <b>Твоя заявка на вход в бота была отклонена.</b>\n\n"
            f"Если произошла ошибка или ты ошибся группой, ты можешь отправить заявку заново по кнопке ниже 👇"
        )
        await bot.send_message(chat_id=target_id, text=student_msg, parse_mode="HTML", reply_markup=kb.rejected_user_kb())
    except Exception:
        pass

@dp.callback_query((F.data == "adm_users_queue") | F.data.startswith("adm_users_queue_"))
async def cb_admin_users_queue(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
        
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "adm_users_queue" else 0
    await render_admin_verification_queue(bot, page)
    await callback.answer("Открыл отдельную очередь верификации.")

@dp.callback_query((F.data == "adm_users_list") | F.data.startswith("adm_users_list_"))
async def cb_admin_users_list(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
        
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "adm_users_list" else 0
    total = await adb.count_verified_users()
    verified = await adb.get_verified_users(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
    if not verified:
        text = "👥 <b>Список верифицированных студентов пуст.</b>"
    else:
        text = f"👥 <b>Верифицированные студенты ({total} чел.)</b> · страница {page + 1}\n\n"
        for i, u in enumerate(verified, page * ADMIN_PAGE_SIZE + 1):
            s_name = html.escape(u.get("student_name") or "Студент")
            s_grp = html.escape(u.get("student_group") or "")
            uname = f" ({html.escape(u['username'])})" if u.get("username") else ""
            text += f"{i}. <b>{s_name}</b> [<code>{s_grp}</code>]{uname}\n"
        text += "\n<i>Студенты получают уведомления при обновлении расписания.</i>"
        
    await callback.message.edit_text(
        text, 
        parse_mode="HTML", 
        reply_markup=kb.admin_verified_users_page_kb(page, total, ADMIN_PAGE_SIZE)
    )
    await callback.answer()

# =========================================================================
# --- АКТУАЛЬНОЕ ОПЕРАТИВНОЕ РАСПИСАНИЕ ПО ГРУППАМ (ПЯТНАШКА) ---
# =========================================================================

async def render_group_schedule_view(
    target,
    user,
    group_name: str,
    is_callback: bool = False,
    bot: Optional[Bot] = None,
    notice: Optional[str] = None,
):
    """Отображает самое актуальное расписание для указанной группы с учётом правила отсечки (17:00 / суббота)"""
    user_id = user.id
    target_chat = target.message.chat if is_callback else target.chat
    is_adm = is_admin(user) and target_chat.type == "private"
    my_group = await get_user_primary_group(user_id)
    is_own = bool(my_group and group_name == my_group)
    
    target_date, is_next_day, reason_text = gdrive_sync.get_target_study_date()
    weekday_idx = target_date.weekday()
    day_name = gdrive_sync.WEEKDAY_NAMES_RU.get(weekday_idx, "")
    date_fmt = target_date.strftime("%d.%m.%Y")
    if is_next_day:
        header_date = f"на {day_name}, {date_fmt}"
        note_line = f"🕒 <i>{reason_text}</i>\n\n"
    else:
        header_date = f"на сегодня ({day_name}, {date_fmt})"
        note_line = ""

    sched = await adb.get_group_schedule(group_name)
    gen_sched = await adb.get_group_schedule("ALL")
    gen_photo = get_general_schedule_photo(gen_sched, target_date=target_date)
    has_gen_photo = bool(gen_photo and group_name != "ALL")
    has_general_schedule = bool(gen_sched and (gen_sched.get("schedule_text") or gen_sched.get("photo_file_id")))
    
    markup = kb.schedule_view_kb(
        group_name=group_name, 
        is_admin_user=is_adm, 
        has_general_photo=has_gen_photo, 
        has_general_schedule=has_general_schedule,
        is_user_own_group=is_own,
        user_own_group=my_group
    )
    
    if sched:
        upd_time = sched.get("updated_at") or "недавно"
        title = html.escape(sched.get("title") or f"Актуальное расписание ({group_name})")
        body = html.escape(sched.get("schedule_text") or "") or "<i>(Информация о парах на фото ниже)</i>"
        safe_group_name = html.escape(group_name)
        
        schedule_heading = (
            f"📢 <b>Общий лист замен колледжа {header_date}</b> 🎓"
            if group_name == "ALL"
            else f"📅 <b>Расписание {header_date} — Группа {safe_group_name}</b> 🎓"
        )
        text = (
            f"{schedule_heading}\n"
            f"{note_line}"
            f"📌 <b>{title}</b>\n\n"
            f"{body}\n\n"
            f"🕒 <i>Обновлено: {upd_time}</i>"
        )
        photo_id = sched.get("photo_file_id")
        if group_name == "ALL" and not photo_id:
            photo_id = gen_photo
    elif gen_sched and group_name != "ALL":
        upd_time = gen_sched.get("updated_at") or "недавно"
        body = html.escape(gen_sched.get("schedule_text") or "") or "<i>(Смотри общий лист замен на фото ниже)</i>"
        safe_group_name = html.escape(group_name)
        text = (
            f"📅 <b>Расписание {header_date} — Группа {safe_group_name}</b> 🎓\n"
            f"{note_line}"
            f"📢 <b>Общий лист замен колледжа:</b>\n\n"
            f"{body}\n\n"
            f"🕒 <i>Обновлено: {upd_time}</i>"
        )
        photo_id = gen_photo
    elif group_name == "ALL" and gen_photo:
        text = (
            f"📢 <b>Общий лист замен колледжа {header_date}</b> 🎓\n"
            f"{note_line}"
            "Исходные фотографии листов замен и консультаций — ниже."
        )
        photo_id = gen_photo
    else:
        text = (
            f"📅 <b>Расписание {header_date} — Группа {html.escape(group_name)}</b> 🎓\n\n"
            f"{note_line}"
            f"⏳ <i>Свежее расписание для этой группы пока не опубликовано.</i>\n\n"
            f"Как только расписание или лист замен появится — тебе сразу придёт оповещение! 📢"
        )
        photo_id = None

    if notice:
        text = f"{notice}\n\n{text}"
        
    actual_bot = bot or getattr(target, "bot", None)
    previous_ui_id = None
    if target_chat.type == "private" and not is_callback:
        previous_ui_id = await adb.get_ui_message_id(user_id)

    async def track_schedule_screen(screen_message):
        if target_chat.type != "private":
            return
        await adb.set_ui_message_id(user_id, screen_message.message_id)
        if previous_ui_id and previous_ui_id != screen_message.message_id:
            try:
                await actual_bot.delete_message(chat_id=target_chat.id, message_id=previous_ui_id)
            except Exception as error:
                logger.debug(
                    "Could not remove previous schedule screen %s for user %s: %s",
                    previous_ui_id,
                    user_id,
                    error,
                )
    
    if isinstance(photo_id, list):
        if is_callback:
            try:
                await target.message.delete()
            except Exception:
                pass
        await actual_bot.send_media_group(
            chat_id=user_id,
            media=build_general_schedule_media(photo_id, target_date=target_date),
        )
        screen_message = await actual_bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        await track_schedule_screen(screen_message)
    elif photo_id:
        if is_callback:
            try:
                await target.message.delete()
            except Exception:
                pass
            screen_message = await actual_bot.send_photo(chat_id=user_id, photo=photo_id, caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            screen_message = await target.answer_photo(photo=photo_id, caption=text, parse_mode="HTML", reply_markup=markup)
        await track_schedule_screen(screen_message)
    else:
        if is_callback:
            await show_user_screen(target, text, reply_markup=markup)
        else:
            await show_user_screen(target, text, reply_markup=markup)

@dp.callback_query(F.data == "schedule_menu")
async def cb_schedule_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    group_name = await get_user_primary_group(callback.from_user.id)
    if not group_name:
        text = (
            "🎓 <b>Выбери свою учебную группу:</b>\n\n"
            "У тебя ещё не выбрана основная группа.\n"
            "Выбери группу из списка колледжа им. А.Е. Ларина, чтобы увидеть расписание 👇"
        )
        markup = kb.schedule_choose_group_kb(
            current_group=None,
            is_admin_user=is_admin(callback.from_user) and callback.message.chat.type == "private"
        )
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.bot.send_message(chat_id=callback.from_user.id, text=text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()
        return
        
    await render_group_schedule_view(callback, callback.from_user, group_name, is_callback=True)
    await callback.answer()

@dp.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext):
    await state.clear()
    if message.chat.type != "private":
        await message.answer("Чтобы открыть расписание своей группы, отправь /schedule в личном чате с ботом.")
        return
    access_ok, err_msg = await check_user_access(message.from_user)
    if not access_ok:
        await prompt_verification_required(message, message.from_user, err_msg)
        return
    group_name = await get_user_primary_group(message.from_user.id)
    if not group_name:
        await show_user_screen(
            message,
            "🎓 <b>Сначала выбери учебную группу:</b>",
            reply_markup=kb.schedule_choose_group_kb(),
        )
        return
    await render_group_schedule_view(message, message.from_user, group_name)

@dp.callback_query(F.data.startswith("sched_view_"))
async def cb_schedule_view(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    group_name = callback.data.replace("sched_view_", "")
    await render_group_schedule_view(callback, callback.from_user, group_name, is_callback=True)
    await callback.answer()

@dp.callback_query(F.data == "sched_choose_group")
async def cb_schedule_choose_group(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    user_id = callback.from_user.id
    my_grp = await get_user_primary_group(user_id)
    is_adm = is_admin(callback.from_user) and callback.message.chat.type == "private"
    
    current_grp_text = f"Твоя текущая группа: <b>{html.escape(my_grp)}</b>\n\n" if my_grp else "Основная группа ещё не выбрана.\n\n"
    text = (
        f"👥 <b>Выбор учебной группы (Пятнашка):</b>\n\n"
        f"{current_grp_text}"
        f"Выбери группу из списка ниже, чтобы увидеть самое актуальное расписание 👇"
    )
    markup = kb.schedule_choose_group_kb(current_group=my_grp, is_admin_user=is_adm)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("sched_set_my_"))
async def cb_schedule_set_my_group(callback: CallbackQuery):
    await callback.answer("Твоя основная группа зафиксирована при верификации.", show_alert=True)

@dp.callback_query(F.data == "sched_bells")
async def cb_schedule_bells(callback: CallbackQuery):
    my_grp = await get_user_primary_group(callback.from_user.id)
    text = db.BELLS_SCHEDULE_TEXT
    back_row = []
    if my_grp:
        back_row.append(InlineKeyboardButton(text=f"🔙 К расписанию {my_grp}", callback_data=f"sched_view_{my_grp}"))
    else:
        back_row.append(InlineKeyboardButton(text="👥 Выбрать группу", callback_data="sched_choose_group"))
    back_row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    markup = InlineKeyboardMarkup(inline_keyboard=[back_row])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.bot.send_message(chat_id=callback.from_user.id, text=text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()

@dp.message(Command("bells", "zvonki"))
async def cmd_bells(message: Message):
    """Быстрый вызов расписания звонков колледжа"""
    my_grp = await get_user_primary_group(message.from_user.id)
    back_row = []
    if my_grp:
        back_row.append(InlineKeyboardButton(text=f"📅 Расписание {my_grp}", callback_data=f"sched_view_{my_grp}"))
    back_row.append(InlineKeyboardButton(text="👥 Выбрать группу", callback_data="sched_choose_group"))
    back_row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    markup = InlineKeyboardMarkup(inline_keyboard=[back_row])
    await show_user_screen(message, db.BELLS_SCHEDULE_TEXT, reply_markup=markup)

@dp.callback_query(F.data == "sched_view_gen_photo")
async def cb_schedule_view_gen_photo(callback: CallbackQuery):
    target_date, is_next_day, reason_text = gdrive_sync.get_target_study_date()
    weekday_idx = target_date.weekday()
    day_name = gdrive_sync.WEEKDAY_NAMES_RU.get(weekday_idx, "")
    date_fmt = target_date.strftime("%d.%m.%Y")
    header_date = f"на {day_name}, {date_fmt}" if is_next_day else f"на сегодня ({day_name}, {date_fmt})"

    gen_sched = await adb.get_group_schedule("ALL")
    photo = get_general_schedule_photo(gen_sched, target_date=target_date)
    if not photo:
        await callback.answer(f"Общий лист замен {header_date} пока не опубликован на Google Диске!", show_alert=True)
        return
        
    my_grp = await get_user_primary_group(callback.from_user.id)
    gen_schedule_text = html.escape((gen_sched or {}).get("schedule_text") or "")
    updated_at = html.escape(str((gen_sched or {}).get("updated_at") or "недавно"))
    note_line = f"🕒 <i>{reason_text}</i>\n\n" if is_next_day else ""
    caption = (
        f"📸 <b>Общий лист замен колледжа {header_date}</b> 🎓\n"
        f"{note_line}"
        f"🕒 <i>Обновлено: {updated_at}</i>\n\n"
        f"{gen_schedule_text or 'В альбоме собраны актуальные фотографии листов замен и консультаций.'}"
    )
    back_kb_rows = []
    if my_grp:
        back_kb_rows.append([InlineKeyboardButton(text=f"🔙 К расписанию группы {my_grp}", callback_data=f"sched_view_{my_grp}")])
    else:
        back_kb_rows.append([InlineKeyboardButton(text="👥 К выбору группы", callback_data="sched_choose_group")])
    back_kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    
    back_markup = InlineKeyboardMarkup(inline_keyboard=back_kb_rows)
    if isinstance(photo, list):
        await callback.bot.send_media_group(
            chat_id=callback.from_user.id,
            media=build_general_schedule_media(photo, target_date=target_date),
        )
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text=caption or "📸 <b>Полный лист замен колледжа</b>",
            parse_mode="HTML",
            reply_markup=back_markup,
        )
    else:
        await callback.bot.send_photo(
            chat_id=callback.from_user.id,
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=back_markup,
        )
    await callback.answer()

# --- Редактирование / публикация расписания админом @Menmamoh ---

@dp.callback_query(F.data == "adm_sched_publish_all")
async def cb_admin_sched_publish_all(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для администратора!", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для публикации расписания.", show_alert=True)
        return

    await state.update_data(target_group="ALL")
    await state.set_state(Form.waiting_for_group_schedule)
    text = (
        "📅 <b>Публикация расписания для всех групп</b>\n\n"
        "Отправь расписание одним сообщением: фотографией с подписью или текстом.\n"
        "Оно появится в расписании у всех групп, а всем верифицированным студентам придёт уведомление.\n\n"
        "<i>Для отмены отправь /cancel.</i>"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_sched_cancel")]
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    except Exception:
        await callback.bot.send_message(chat_id=callback.from_user.id, text=text, parse_mode="HTML", reply_markup=cancel_kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_sched_cancel")
async def cb_admin_sched_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await show_admin_panel(callback, callback.from_user, bot, is_callback=True)
    await callback.answer("Публикация отменена")

@dp.callback_query(F.data.startswith("adm_sched_edit_"))
async def cb_admin_sched_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для изменения расписания.", show_alert=True)
        return
        
    target_group = callback.data.replace("adm_sched_edit_", "")
    if target_group not in {*db.COLLEGE_GROUPS, "ALL"}:
        await callback.answer("Неизвестная группа расписания.", show_alert=True)
        return
    await state.update_data(target_group=target_group)
    await state.set_state(Form.waiting_for_group_schedule)
    
    text = (
        f"✏️ <b>Обновление расписания — Группа {html.escape(target_group)}</b> 🎓\n\n"
        f"Отправь самое актуальное расписание на сегодня/ближайшие дни:\n"
        f"📸 <b>Фото листа замен/расписания</b> (можно сразу с текстом в подписи!)\n"
        f"💬 Или просто <b>текстовое сообщение</b> со списком пар и кабинетами.\n\n"
        f"<b>Пример текста:</b>\n"
        f"<code>1. Технология отделки мебели (каб. 204)\n"
        f"2. Экономика организации (каб. 105)\n"
        f"3. Замена: ТПМ вместо дизайна (каб. 214)</code>\n\n"
        f"📢 <i>Сразу после отправки бот автоматически разошлёт оповещение всем верифицированным студентам этой группы!</i>\n\n"
        f"<i>(или отправь /cancel для отмены)</i>"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"sched_view_{target_group}")]
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.bot.send_message(chat_id=callback.from_user.id, text=text, parse_mode="HTML", reply_markup=cancel_kb)
    await callback.answer()

@dp.message(Form.waiting_for_group_schedule)
async def process_group_schedule_input(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    data = await state.get_data()
    target_group = data.get("target_group")
    if target_group not in {*db.COLLEGE_GROUPS, "ALL"}:
        await state.clear()
        await show_user_screen(message, "⚠️ Сеанс редактирования расписания завершён. Открой редактирование заново.", reply_markup=kb.back_to_menu_kb())
        return
    
    photo_file_id = message.photo[-1].file_id if message.photo else None
    raw_text = (message.caption if message.photo else message.text) or ""
    raw_text = raw_text.strip()
    
    if not photo_file_id and not raw_text:
        await show_user_screen(message, "⚠️ Отправь фотографию расписания или текстовое сообщение:", reply_markup=kb.back_to_menu_kb())
        return
        
    title = f"Замена / Расписание ({target_group})"
    admin_tag = f"@{message.from_user.username or ADMIN_USERNAME}"
    
    await adb.set_group_schedule(
        group_name=target_group,
        schedule_text=raw_text,
        title=title,
        photo_file_id=photo_file_id,
        updated_by=admin_tag
    )
    await state.clear()
    
    # Автоматическая рассылка верифицированным студентам
    safe_target_group = html.escape(target_group)
    if target_group == "ALL":
        broadcast_header = "📢 <b>Опубликовано новое расписание для всех групп!</b> 🎓\n\n"
    else:
        broadcast_header = f"📢 <b>Новое расписание для группы {safe_target_group}!</b> 🎓\n\n"

    broadcast_body = (
        f"{broadcast_header}"
        "Расписание обновлено. Открой бота, чтобы посмотреть текст и фотографии листа."
    )
    broadcast_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 Расписание ({target_group})", callback_data=f"sched_view_{target_group}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    sent_count = 0
    failed_count = 0
    semaphore = asyncio.Semaphore(8)
    send_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    last_send_at = 0.0

    async def send_update(user_id: int) -> bool:
        nonlocal last_send_at
        async with semaphore:
            async with send_lock:
                delay = 0.05 - (loop.time() - last_send_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                last_send_at = loop.time()
            for attempt in range(2):
                try:
                    if photo_file_id:
                        await bot.send_photo(
                            chat_id=user_id, photo=photo_file_id, caption=broadcast_body,
                            parse_mode="HTML", reply_markup=broadcast_kb,
                        )
                    else:
                        await bot.send_message(
                            chat_id=user_id, text=broadcast_body,
                            parse_mode="HTML", reply_markup=broadcast_kb,
                        )
                    return True
                except TelegramRetryAfter as error:
                    if attempt == 0:
                        await asyncio.sleep(error.retry_after)
                    else:
                        logger.warning("Schedule broadcast rate-limited for user %s", user_id)
                except Exception:
                    logger.exception("Could not broadcast schedule update to user %s", user_id)
                    break
        return False

    offset = 0
    batch_size = 200
    while True:
        if target_group == "ALL":
            target_users = await adb.get_verified_users(batch_size, offset)
        else:
            target_users = await adb.get_verified_users_by_group(target_group, batch_size, offset)
        if not target_users:
            break
        user_ids = [user["user_id"] for user in target_users if user["user_id"] != message.from_user.id]
        results = await asyncio.gather(*(send_update(user_id) for user_id in user_ids))
        sent_count += sum(results)
        failed_count += len(results) - sum(results)
        offset += len(target_users)

    info_str = (
        f"📢 Доставлено: <b>{sent_count}</b>; ошибок отправки: <b>{failed_count}</b>."
        if sent_count or failed_count
        else ("📢 Пока нет других верифицированных студентов." if target_group == "ALL"
              else f"📢 В группе {safe_target_group} пока нет других верифицированных студентов.")
    )
    published_scope = "всех групп" if target_group == "ALL" else f"группы {safe_target_group}"
    confirm_text = f"✅ <b>Расписание для {published_scope} успешно обновлено!</b>\n{info_str}"
    # Отображаем обновленный экран расписания
    await render_group_schedule_view(message, message.from_user, target_group, is_callback=False, bot=bot, notice=confirm_text)

@dp.callback_query(F.data.startswith("adm_sched_clear_"))
async def cb_admin_sched_clear(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для изменения расписания.", show_alert=True)
        return
        
    target_group = callback.data.replace("adm_sched_clear_", "")
    if target_group not in {*db.COLLEGE_GROUPS, "ALL"}:
        await callback.answer("Неизвестная группа расписания.", show_alert=True)
        return
    await adb.clear_group_schedule(target_group)
    await callback.answer(f"Расписание для {target_group} очищено!", show_alert=True)
    await render_group_schedule_view(callback, callback.from_user, target_group, is_callback=True)

@dp.callback_query(F.data == "adm_gdrive_sync")
async def cb_admin_gdrive_sync(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для синхронизации.", show_alert=True)
        return

    await callback.answer("⏳ Синхронизация с Google Диском...", show_alert=False)
    status_msg = await callback.message.answer(
        "🔄 <i>Подключаюсь к Google Диску колледжа и проверяю свежие замены...</i>", 
        parse_mode="HTML"
    )

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, gdrive_sync.sync_drive_schedule)

    if not res.get("ok"):
        await status_msg.edit_text(
            f"⚠️ <b>Ошибка при синхронизации с Google Диском:</b>\n{res.get('error')}",
            parse_mode="HTML"
        )
        return

    total = res.get("total_found", 0)
    new_dl = res.get("new_downloaded", 0)
    files = res.get("files", [])

    lines = []
    for f in files:
        d_str = f.get("schedule_date") or "—"
        doc_t = "Замены" if f.get("doc_type") == "replacement" else "Консультации"
        part = f" (часть {f['part_number']})" if f.get("part_number", 1) > 1 else ""
        lines.append(f"• <b>{doc_t}</b> на <code>{d_str}</code>{part}: {html.escape(f['filename'])}")

    report = (
        f"✅ <b>Синхронизация с Google Диском успешно завершена!</b>\n\n"
        f"📁 Всего файлов найдено: <b>{total}</b>\n"
        f"📥 Новых скачано: <b>{new_dl}</b>\n\n"
        f"📋 <b>Актуальные документы:</b>\n" + ("\n".join(lines) if lines else "<i>(файлы не найдены)</i>") + "\n\n"
        f"💡 <i>Свежие листы замен теперь доступны всем студентам во вкладке «📅 Расписание»!</i>"
    )
    await status_msg.edit_text(report, parse_mode="HTML")


# Совместимость со старыми кнопками
@dp.callback_query(F.data.in_(["sched_today", "sched_tomorrow", "sched_week"]) | F.data.startswith("sched_day_"))
async def cb_legacy_schedule(callback: CallbackQuery, state: FSMContext):
    await cb_schedule_menu(callback, state)

# =========================================================================
# --- ТРЕКЕР ДОЛГОВ И УЧЕБНЫХ РАБОТ (ХАБ) ---
# =========================================================================

@dp.callback_query(F.data == "debts_hub")
async def cb_debts_hub(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    u_rec = await adb.get_user(user_id)
    s_grp = u_rec.get("student_group") if u_rec else None
    grp_badge = f" ({s_grp})" if s_grp else ""
    summary = await adb.get_all_debts_summary(user_id)
    
    text = (
        f"📝 <b>Трекер долгов и учебных работ{grp_badge}</b> 🎓\n\n"
        f"Здесь ты можешь отмечать сданные лабы, следить за хвостами и добавлять свои предметы.\n\n"
        f"📊 <b>Твой прогресс сдачи:</b>\n"
        f"• Всего работ в базе: <b>{summary['total']}</b>\n"
        f"• Сдано работ: <b>{summary['total'] - summary['pending']}</b>\n"
        f"• Осталось сдать (долгов): <b>{summary['pending']}</b>\n"
        f"• Общий прогресс: <code>[{summary['bar']}]</code> <b>{summary['percent']}%</b>\n\n"
        f"Выбери нужный раздел 👇"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.debts_hub_kb())
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb.debts_hub_kb())
    await callback.answer()

# --- Показать все долги ---
@dp.callback_query(F.data == "show_all_debts")
async def cb_show_all_debts(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    summary = await adb.get_all_debts_summary(user_id)
    
    if summary["total"] == 0:
        text = (
            "🎉 <b>У тебя пока нет добавленных работ!</b>\n\n"
            "Выбери предмет в списке и нажми <i>«➕ Добавить работу»</i>, чтобы внести лабы или домашки."
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.back_to_debts_hub_kb())
        await callback.answer()
        return

    if summary["pending"] == 0:
        text = (
            "🔥 <b>ЧИСТО! Долгов нет вообще!</b> 🥳\n\n"
            f"Все <b>{summary['total']}</b> работ сданы на 100%!\n"
            f"Прогресс: <code>[{summary['bar']}]</code> <b>100%</b>"
        )
    else:
        text = (
            f"📋 <b>СВОДКА ВСЕХ ДОЛГОВ ПО УЧЁБЕ</b>\n\n"
            f"🔴 <b>Осталось сдать:</b> {summary['pending']} из {summary['total']} работ\n"
            f"📈 <b>Прогресс закрытия:</b> <code>[{summary['bar']}]</code> {summary['percent']}%\n\n"
            f"─── <b>СПИСОК ХВОСТОВ:</b> ───\n"
        )
        for subject_name, tasks in summary["by_subject"].items():
            safe_sub = html.escape(subject_name)
            text += f"\n📚 <b>{safe_sub}</b> ({len(tasks)}):\n"
            for t in tasks:
                safe_task = html.escape(t)
                text += f"  ❌ {safe_task}\n"
        
        text += "\n💡 <i>Чтобы отметить работу или посмотреть методички, перейди в «Мои предметы и работы».</i>"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.back_to_debts_hub_kb())
    await callback.answer()

# --- Список предметов ---
@dp.callback_query(F.data == "list_subjects")
async def cb_list_subjects(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    subjects = await adb.get_subjects_with_counts(user_id)
    
    text = (
        "📚 <b>Выбери предмет</b>, чтобы посмотреть работы, методички или отметить сданные:\n\n"
        "🔴 — есть незакрытые долги\n"
        "🟢 — все работы сданы (или пока нет добавленных)"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.subjects_list_kb(subjects))
    await callback.answer()

# --- Открыть предмет и его работы ---
@dp.callback_query(F.data.startswith("open_sub_"))
async def cb_open_subject(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    parts = callback.data.split("_")
    subject_id = int(parts[2])
    page = max(0, int(parts[3])) if len(parts) > 3 else 0
    page_size = 25
    
    subject_page = await adb.get_subject_tasks_page(user_id, subject_id, page_size, page * page_size)
    if not subject_page:
        await callback.answer("Предмет не найден!", show_alert=True)
        return
    subject = subject_page["subject"]
    tasks = subject_page["tasks"]
    total_tasks = subject_page["total"]
    pending_count = subject_page["pending"]
    safe_name = html.escape(subject["name"])
    
    # Загружаем все счётчики материалов предмета одним запросом.
    global_counts = await adb.get_global_material_counts_for_subject(subject["name"])
    mat_counts = {t["id"]: global_counts.get(t["title"].strip().lower(), 0) for t in tasks}
    
    text = (
        f"📖 <b>Предмет:</b> {safe_name}\n"
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (долгов: <b>{pending_count}</b>)\n\n"
        f"• Нажатие <b>слева</b> переключает статус сдачи (❌ / ✅)\n"
        f"• Кнопка <b>справа 📚</b> открывает проверенные методички и решения к работе!"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.subject_tasks_kb(subject_id, tasks, mat_counts, page, total_tasks, page_size))
    await callback.answer()

# --- Переключение статуса работы (TOGGLE) ---
@dp.callback_query(F.data.startswith("toggle_t_"))
async def cb_toggle_task(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    parts = callback.data.split("_")
    task_id = int(parts[2])
    subject_id = int(parts[3])
    page = max(0, int(parts[4])) if len(parts) > 4 else 0
    page_size = 25
    
    subject = await adb.get_subject_by_id(user_id, subject_id)
    if not subject:
        await callback.answer("Предмет не найден или уже удалён.", show_alert=True)
        return
    new_status, tasks, total_tasks, pending_count = await adb.toggle_task_status_and_get_tasks(
        user_id, subject_id, task_id, page_size, page * page_size
    )
    if new_status is None:
        await callback.answer("Работа не найдена в этом предмете.", show_alert=True)
        return
    safe_name = html.escape(subject["name"])
    global_counts = await adb.get_global_material_counts_for_subject(subject["name"])
    mat_counts = {t["id"]: global_counts.get(t["title"].strip().lower(), 0) for t in tasks}
    
    status_msg = "✅ Засчитано!" if new_status else "❌ Снова в долгах!"
    
    text = (
        f"📖 <b>Предмет:</b> {safe_name}\n"
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (долгов: <b>{pending_count}</b>)\n\n"
        f"• Нажатие <b>слева</b> переключает статус сдачи (❌ / ✅)\n"
        f"• Кнопка <b>справа 📚</b> открывает проверенные методички и решения к работе!"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.subject_tasks_kb(subject_id, tasks, mat_counts, page, total_tasks, page_size))
    await callback.answer(status_msg)

# =========================================================================
# --- БЛОК: МЕТОДИЧКИ И РЕШЕНИЯ (МАТЕРИАЛЫ) ---
# =========================================================================

@dp.callback_query(F.data.startswith("view_mat_"))
async def cb_view_materials(callback: CallbackQuery):
    parts = callback.data.split("_")
    task_id = int(parts[2])
    subject_id = int(parts[3])
    page = max(0, int(parts[4])) if len(parts) > 4 else 0
    
    user_id = callback.from_user.id
    subject = await adb.get_subject_by_id(user_id, subject_id)
    if not subject:
        await callback.answer("Работа не найдена в этом предмете.", show_alert=True)
        return
    task_info = await adb.get_task_info_for_subject(user_id, subject_id, task_id)
    if not task_info:
        await callback.answer("Работа не найдена!", show_alert=True)
        return
        
    # Ищем материалы глобально по названию предмета и работы
    material_total = await adb.count_materials_for_task_globally(task_info["subject_name"], task_info["title"])
    materials = await adb.get_materials_for_task_globally(
        task_info["subject_name"], task_info["title"], limit=25, offset=page * 25
    )
    safe_title = html.escape(task_info["title"])
    safe_sub = html.escape(task_info["subject_name"])
    
    if not materials:
        text = (
            f"📚 <b>Материалы к работе:</b>\n"
            f"«{safe_title}» (<i>{safe_sub}</i>)\n\n"
            f"Пока никто не добавил проверенных методичек или решений к этой работе.\n\n"
            f"Будь первым! Нажми <b>«📤 Предложить методичку / решение»</b>, загрузи фото или файл, "
            f"и после быстрой верификации он станет доступен всем студентам!"
        )
    else:
        text = (
            f"📚 <b>Проверенные материалы к работе:</b>\n"
            f"«{safe_title}» (<i>{safe_sub}</i>)\n\n"
            f"Нажми на кнопку материала ниже, чтобы скачать методичку или готовое решение 👇"
        )
        
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=kb.materials_list_kb(task_id, subject_id, materials, page, material_total, 25)
    )
    await callback.answer()

# --- Скачивание / получение материала ---
@dp.callback_query(F.data.startswith("dl_mat_"))
async def cb_download_material(callback: CallbackQuery):
    parts = callback.data.split("_")
    mat_id = int(parts[2])
    
    mat = await adb.get_material_by_id(mat_id)
    if not mat or mat.get("status") != "approved":
        await callback.answer("Материал не найден!", show_alert=True)
        return
        
    task_info = await adb.get_task_info(mat["task_id"])
    subject_name = task_info["subject_name"] if task_info else "предмет"
    task_title = task_info["title"] if task_info else "Работа"
    
    icon = "📖" if mat["material_type"] == "method" else "💡"
    type_name = "Методичка" if mat["material_type"] == "method" else "Решение"
    author = html.escape(mat["user_name"] or "Аноним")
    title = html.escape(mat["title"])
    
    caption = (
        f"{icon} <b>{type_name}:</b> {title}\n"
        f"📚 <b>Предмет:</b> {html.escape(subject_name)}\n"
        f"📝 <b>Работа:</b> {html.escape(task_title)}\n"
        f"👤 <i>Добавил: {author} (проверено модератором ✅)</i>"
    )
    is_adm = is_admin(callback.from_user)
    reply_markup = kb.material_downloaded_kb(subject_name, material_id=mat_id, is_admin_user=is_adm)
    
    if mat["file_type"] == "document" and mat["file_id"]:
        await callback.message.answer_document(document=mat["file_id"], caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    elif mat["file_type"] == "photo" and mat["file_id"]:
        await callback.message.answer_photo(photo=mat["file_id"], caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    elif mat["content_text"]:
        await callback.message.answer(f"{caption}\n\n📄 <b>Текст / Ссылка:</b>\n{html.escape(mat['content_text'])}", parse_mode="HTML", reply_markup=reply_markup)
        
    await callback.answer("Отправлено в чат!")

# =========================================================================
# --- УДАЛЕНИЕ НЕАКТУАЛЬНЫХ МАТЕРИАЛОВ (ДЛЯ АДМИНИСТРАТОРА @Menmamoh) ---
# =========================================================================

@dp.callback_query(F.data.startswith("adm_ask_del_"))
async def cb_admin_ask_delete_material(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только @Menmamoh может удалять материалы!", show_alert=True)
        return
        
    mat_id = int(callback.data.replace("adm_ask_del_", ""))
    mat = await adb.get_material_by_id(mat_id)
    if not mat:
        await callback.answer("Материал уже удален!", show_alert=True)
        return
        
    safe_title = html.escape(mat["title"])
    sub_name = html.escape(mat.get("subject_name") or "предмет")
    text = (
        f"🗑️ <b>Подтверждение удаления материала</b>\n\n"
        f"📚 <b>Предмет:</b> {sub_name}\n"
        f"📄 <b>Материал:</b> «{safe_title}»\n\n"
        f"Ты точно хочешь навсегда удалить этот материал?\n\n"
        f"⚠️ После удаления он будет стерт из базы и перестанет показываться студентам."
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.admin_confirm_delete_material_kb(mat_id))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.admin_confirm_delete_material_kb(mat_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_do_del_"))
async def cb_admin_do_delete_material(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только @Menmamoh может удалять материалы!", show_alert=True)
        return
        
    mat_id = int(callback.data.replace("adm_do_del_", ""))
    mat = await adb.get_material_by_id(mat_id)
    if not mat or not await adb.delete_material(mat_id):
        await callback.answer("Материал уже удален.", show_alert=True)
        return
    title = mat["title"]
    
    safe_title = html.escape(title)
    text = (
        f"🗑️ <b>Материал успешно удален!</b>\n\n"
        f"«{safe_title}» стерт из базы данных и больше не доступен студентам."
    )
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗄️ К списку предметов (удалить ещё)", callback_data="adm_manage_materials")],
        [InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_panel")]
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    await callback.answer("Удалено!")

@dp.callback_query(F.data == "adm_manage_materials")
async def cb_admin_manage_materials(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только @Menmamoh имеет доступ!", show_alert=True)
        return
        
    known_subjects = await adb.get_all_known_subject_names(callback.from_user.id)
    counts_map = await adb.get_global_material_counts_by_subject()
    
    subjects_with_counts = []
    seen = set()
    for name in known_subjects:
        cnt = counts_map.get(name.strip().lower(), 0)
        subjects_with_counts.append({"name": name, "count": cnt})
        seen.add(name.strip().lower())
        
    for sub_lower, cnt in counts_map.items():
        if sub_lower not in seen and cnt > 0:
            subjects_with_counts.append({"name": sub_lower.capitalize(), "count": cnt})
            seen.add(sub_lower)
            
    total_mats = sum(counts_map.values())
    if total_mats == 0:
        text = (
            "🗄️ <b>Управление базой знаний (Удаление неактуального)</b>\n\n"
            "В базе сейчас <b>нет опубликованных материалов</b>.\n\n"
            "Когда студенты добавят методички или решения и они будут одобрены, "
            "здесь появится список предметов для быстрого удаления устаревших файлов."
        )
    else:
        text = (
            "🗄️ <b>Управление базой знаний (Удаление неактуального)</b>\n\n"
            f"Всего в базе проверенных материалов: <b>{total_mats}</b> шт.\n\n"
            "Выбери предмет, чтобы просмотреть опубликованные материалы и удалить устаревшие 👇"
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.admin_materials_subjects_kb(subjects_with_counts))
    await callback.answer()

async def _resolve_subject_callback(token: str) -> Optional[str]:
    for name in await adb.get_all_known_subject_names():
        if kb.subject_callback_token(name) == token:
            return name
    return None


@dp.callback_query(F.data.startswith("adm_sub_mats_"))
async def cb_admin_sub_mats(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Доступ только для @Menmamoh!", show_alert=True)
        return
        
    callback_value = callback.data.replace("adm_sub_mats_", "", 1)
    subject_name = await _resolve_subject_callback(callback_value) or callback_value
    materials = await adb.get_approved_materials_by_subject_name(subject_name)
    safe_name = html.escape(subject_name)
    
    if not materials:
        text = f"В предмете <b>«{safe_name}»</b> сейчас нет опубликованных материалов."
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.admin_panel_kb())
        await callback.answer()
        return
        
    text = (
        f"🗄️ <b>Материалы предмета «{safe_name}»</b>\n\n"
        f"Найдено материалов: <b>{len(materials)}</b> шт.\n\n"
        f"Нажми на кнопку материала, который нужно <b>удалить</b> из базы 👇"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.admin_subject_materials_delete_kb(subject_name, materials))
    await callback.answer()

# =========================================================================
# --- БАЗА ЗНАНИЙ (ОБЩАЯ БИБЛИОТЕКА МЕТОДИЧЕК И РЕШЕНИЙ) ---
# =========================================================================

@dp.callback_query(F.data == "browse_materials_hub")
async def cb_browse_materials_hub(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    u_rec = await adb.get_user(user_id)
    s_grp = u_rec.get("student_group") if u_rec else None
    grp_badge = f" ({s_grp})" if s_grp else ""
    await adb.seed_default_subjects(user_id, student_group=s_grp)
    known_subjects = await adb.get_all_known_subject_names(user_id)
    counts_map = await adb.get_global_material_counts_by_subject()
    
    subjects_with_counts = [
        {"name": name, "count": counts_map.get(name.lower(), 0)}
        for name in known_subjects
    ]
    total_approved = sum(counts_map.values())
    
    text = (
        f"📖 <b>База методичек и готовых решений{grp_badge}</b> 🎓\n\n"
        f"Здесь собраны все проверенные материалы по учёбе нашей группы (всего доступно: <b>{total_approved}</b> шт.).\n\n"
        "🟢 — есть проверенные материалы\n"
        "⚪ — пока пусто (поделись с группой!)\n\n"
        "Выбери предмет, чтобы открыть список файлов 👇"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.materials_hub_kb(subjects_with_counts))
    await callback.answer()

@dp.callback_query(F.data.startswith("b_sub_"))
async def cb_browse_subject_materials(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    callback_value = callback.data.replace("b_sub_", "", 1)
    subject_name = await _resolve_subject_callback(callback_value) or callback_value
    materials = await adb.get_approved_materials_by_subject_name(subject_name)
    safe_name = html.escape(subject_name)
    
    if not materials:
        text = (
            f"📖 <b>Предмет: «{safe_name}»</b>\n\n"
            f"Пока никто не добавил проверенных методичек или готовых решений по этому предмету.\n\n"
            f"Будь первым, кто поделится с группой! Загрузи фото или файл 👇"
        )
    else:
        text = (
            f"📖 <b>Материалы по предмету «{safe_name}»:</b>\n\n"
            f"Найдено проверенных материалов: <b>{len(materials)}</b>\n\n"
            f"Нажми на нужный материал ниже, чтобы открыть его прямо в чате 👇"
        )
        
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.subject_materials_view_kb(subject_name, materials))
    await callback.answer()

@dp.callback_query(F.data.startswith("quick_sub_name_"))
async def cb_quick_sub_by_name(callback: CallbackQuery, state: FSMContext):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    callback_value = callback.data.replace("quick_sub_name_", "", 1)
    subject_name = await _resolve_subject_callback(callback_value) or callback_value
    user_id = callback.from_user.id
    u_rec = await adb.get_user(user_id)
    s_grp = u_rec.get("student_group") if u_rec else None
    await adb.seed_default_subjects(user_id, student_group=s_grp)
    subjects = await adb.get_subjects_with_counts(user_id)
    sub = next((s for s in subjects if s["name"].lower() == subject_name.lower()), None)
    if sub:
        callback.data = f"quick_sub_{sub['id']}"
        await cb_quick_sub_select(callback, state)
    else:
        await cb_quick_upload_material(callback, state)

# =========================================================================
# --- БЫСТРАЯ ЗАГРУЗКА МАТЕРИАЛОВ С ГЛАВНОГО МЕНЮ ---
# =========================================================================

@dp.callback_query(F.data == "quick_upload_material")
async def cb_quick_upload_material(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, err_msg = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, err_msg)
        return
        
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    u_rec = await adb.get_user(user_id)
    s_grp = u_rec.get("student_group") if u_rec else None
    grp_badge = f" ({s_grp})" if s_grp else ""
    await adb.seed_default_subjects(user_id, student_group=s_grp)
    subjects = await adb.get_subjects_with_counts(user_id)
    
    text = (
        f"📤 <b>Добавление методички или решения{grp_badge}</b>\n\n"
        "Поделись материалами к учёбе с одногруппниками! После верификации администратором "
        "материал появится в общей базе бота и поможет всем закрыть долги.\n\n"
        "Выбери предмет из списка ниже 👇"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.quick_subjects_select_kb(subjects))
    await callback.answer()

@dp.callback_query(F.data.startswith("quick_sub_"))
async def cb_quick_sub_select(callback: CallbackQuery, state: FSMContext):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    parts = callback.data.split("_")
    subject_id = int(parts[2])
    page = max(0, int(parts[3])) if len(parts) > 3 else 0
    user_id = callback.from_user.id
    subject = await adb.get_subject_by_id(user_id, subject_id)
    if not subject:
        await callback.answer("Предмет не найден!", show_alert=True)
        return
        
    page_size = 25
    subject_page = await adb.get_subject_tasks_page(user_id, subject_id, page_size, page * page_size)
    total_tasks = subject_page["total"]
    tasks = subject_page["tasks"]
    safe_name = html.escape(subject["name"])
    
    if not tasks:
        await state.update_data(subject_id=subject_id)
        await state.set_state(Form.waiting_for_quick_task_title)
        text = (
            f"📚 В предмете <b>«{safe_name}»</b> пока нет добавленных работ.\n\n"
            f"✍️ <b>Введи название работы или методички</b>, которую хочешь загрузить:\n"
            f"(например, <code>Общая методичка по предмету</code> или <code>Лабораторная №1</code>)\n\n"
            f"<i>(или отправь /cancel для отмены)</i>"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.back_to_menu_kb())
    else:
        text = (
            f"📚 <b>Предмет:</b> {safe_name}\n\n"
            f"Выбери работу, к которой хочешь прикрепить методичку или решение 👇"
        )
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=kb.quick_tasks_select_kb(subject_id, tasks, page, total_tasks, page_size)
        )
        
    await callback.answer()

@dp.callback_query(F.data.startswith("quick_task_"))
async def cb_quick_task_select(callback: CallbackQuery):
    parts = callback.data.split("_")
    task_id = int(parts[2])
    subject_id = int(parts[3])
    
    task_info = await adb.get_task_info_for_subject(callback.from_user.id, subject_id, task_id)
    if not task_info:
        await callback.answer("Работа не найдена!", show_alert=True)
        return
        
    safe_title = html.escape(task_info["title"])
    safe_sub = html.escape(task_info["subject_name"])
    
    text = (
        f"📤 <b>Загрузка материала к работе:</b>\n"
        f"«{safe_title}» (<i>{safe_sub}</i>)\n\n"
        f"• <b>📖 Методичка</b> — задание, методические указания, теория.\n"
        f"• <b>💡 Решение</b> — готовая работа, правильный вариант, расчетка, код.\n\n"
        f"<i>Все материалы проходят верификацию перед публикацией.</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.choose_material_type_kb(task_id, subject_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("quick_new_task_"))
async def cb_quick_new_task(callback: CallbackQuery, state: FSMContext):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    subject_id = int(callback.data.replace("quick_new_task_", ""))
    user_id = callback.from_user.id
    subject = await adb.get_subject_by_id(user_id, subject_id)
    if not subject:
        await callback.answer("Предмет не найден!", show_alert=True)
        return
        
    await state.update_data(subject_id=subject_id)
    await state.set_state(Form.waiting_for_quick_task_title)
    
    safe_name = html.escape(subject["name"])
    text = (
        f"✍️ <b>Введи название работы для предмета «{safe_name}»</b>:\n\n"
        f"Например: <code>Лабораторная работа №2</code> или <code>Методические указания</code>.\n\n"
        f"<i>(или отправь /cancel для отмены)</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.back_to_menu_kb())
    await callback.answer()

@dp.message(Form.waiting_for_quick_task_title)
async def process_quick_task_title(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    data = await state.get_data()
    subject_id = data.get("subject_id")
    user_id = message.from_user.id
    
    raw_title = ""
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
        raw_title = message.caption.strip() if message.caption else "Лабораторная работа"
    elif message.document:
        photo_id = message.document.file_id
        raw_title = message.caption.strip() if message.caption else (message.document.file_name or "Новый документ")
    elif message.text:
        raw_title = message.text.strip()
        
    if not raw_title:
        await show_user_screen(message, "⚠️ Отправь название текстом или прикрепи фотографию/файл с подписью.", reply_markup=kb.back_to_menu_kb())
        return
    subject = await adb.get_subject_by_id(user_id, subject_id) if subject_id else None
    if not subject:
        await state.clear()
        await show_user_screen(message, "⚠️ Предмет не найден или уже удалён. Начни загрузку заново.", reply_markup=kb.back_to_menu_kb())
        return

    task_id = await adb.add_task(user_id, subject_id, raw_title)
    if task_id is None:
        await state.clear()
        await show_user_screen(message, "⚠️ Не удалось создать работу. Проверь предмет и попробуй ещё раз.", reply_markup=kb.back_to_menu_kb())
        return
    safe_sub = html.escape(subject["name"])
    safe_title = html.escape(raw_title)
    
    # Если вместе с названием сразу скинули фото — сохраняем фото в state!
    if photo_id:
        await state.update_data(
            subject_id=subject_id,
            task_id=task_id,
            unsolicited_photo_id=photo_id,
            unsolicited_caption=raw_title
        )
        text = (
            f"✅ Работа <b>«{safe_title}»</b> ({safe_sub}) создана и фото получено!\n\n"
            f"📤 <b>Что это за материал?</b>\n"
            f"• <b>📖 Методичка</b> — задание / указания.\n"
            f"• <b>💡 Решение</b> — готовая работа / расчетка / шпора."
        )
        await show_user_screen(message, text, reply_markup=kb.choose_material_type_kb(task_id, subject_id))
        return
        
    # Если прислали просто название текстом:
    await state.clear()
    text = (
        f"✅ Работа <b>«{safe_title}»</b> ({safe_sub}) создана!\n\n"
        f"📤 <b>Что ты хочешь прикрепить к этой работе?</b>\n\n"
        f"• <b>📖 Методичка</b> — официальное задание, требования преподавателя, методические указания.\n"
        f"• <b>💡 Решение</b> — готовая работа, правильный вариант, расчетка, шпаргалка или код.\n\n"
        f"<i>Все материалы проходят верификацию перед публикацией.</i>"
    )
    await show_user_screen(message, text, reply_markup=kb.choose_material_type_kb(task_id, subject_id))

# --- Начать процесс предложения материала из карточки работы ---
@dp.callback_query(F.data.startswith("upload_mat_"))
async def cb_upload_material(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    parts = callback.data.split("_")
    task_id = int(parts[2])
    subject_id = int(parts[3])
    task_info = await adb.get_task_info_for_subject(callback.from_user.id, subject_id, task_id)
    if not task_info:
        await callback.answer("Работа не найдена в этом предмете.", show_alert=True)
        return
    
    text = (
        "📤 <b>Что ты хочешь предложить к этой работе?</b>\n\n"
        "• <b>📖 Методичка</b> — официальное задание, требования преподавателя, методические указания.\n"
        "• <b>💡 Решение</b> — готовая работа, правильный вариант, расчетка, шпаргалка или код.\n\n"
        "<i>Все материалы проходят верификацию перед публикацией.</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.choose_material_type_kb(task_id, subject_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mat_type_"))
async def cb_set_material_type(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    parts = callback.data.split("_")
    mat_type = parts[3] # 'method' or 'solution'
    task_id = int(parts[4])
    subject_id = int(parts[5])
    task_info = await adb.get_task_info_for_subject(callback.from_user.id, subject_id, task_id)
    if mat_type not in {"method", "solution"} or not task_info:
        await callback.answer("Работа не найдена в этом предмете.", show_alert=True)
        return
    
    data = await state.get_data()
    unsolicited_photo_id = data.get("unsolicited_photo_id")
    unsolicited_caption = data.get("unsolicited_caption")
    
    # Если пользователь ранее просто скинул фото в чат и выбрал для него предмет/работу:
    if unsolicited_photo_id:
        user_id = callback.from_user.id
        user_name = callback.from_user.username or callback.from_user.full_name or f"User_{user_id}"
        if callback.from_user.username:
            user_name = f"@{callback.from_user.username}"
            
        default_title = "Фото методички" if mat_type == "method" else "Фото решения"
        title = unsolicited_caption if unsolicited_caption else default_title
        
        mat_id = await adb.add_material(
            user_id=user_id,
            user_name=user_name,
            task_id=task_id,
            material_type=mat_type,
            title=title,
            file_id=unsolicited_photo_id,
            file_type="photo",
            content_text=None
        )
        if mat_id is None:
            await callback.answer("Работа не найдена или уже удалена.", show_alert=True)
            return
        await state.clear()
        
        task_info = await adb.get_task_info(task_id)
        safe_sub = html.escape(task_info["subject_name"]) if task_info else ""
        safe_task = html.escape(task_info["title"]) if task_info else ""
        
        # 1. Сообщение пользователю с обратной связью от Менмы
        await callback.message.edit_text(
            "🚀 <b>Фотография успешно отправлена!</b>\n\n"
            "💬 <b>Сообщение от Менмы:</b>\n"
            "<i>«Щас проверю 😎🤙»</i>\n\n"
            "⏳ Твоё фото передано на верификацию к @Menmamoh. Как только он нажмёт «Одобрить», оно появится в боте у всех студентов!",
            parse_mode="HTML",
            reply_markup=kb.material_sent_success_kb(subject_id)
        )
        
        # 2. Уведомление Менме
        admin_id = await adb.get_admin_id() or ADMIN_ID
        if admin_id:
            icon = "📖" if mat_type == "method" else "💡"
            type_str = "Методичка" if mat_type == "method" else "Решение"
            adm_text = (
                f"🔔 <b>НОВЫЙ МАТЕРИАЛ НА ВЕРИФИКАЦИЮ!</b>\n\n"
                f"👤 <b>От:</b> {html.escape(user_name)} (<code>{user_id}</code>)\n"
                f"📚 <b>Предмет:</b> {safe_sub}\n"
                f"📝 <b>Работа:</b> {safe_task}\n"
                f"🏷️ <b>Тип:</b> {icon} {type_str}\n"
                f"📄 <b>Название:</b> {html.escape(title)}\n"
            )
            try:
                await bot.send_photo(chat_id=admin_id, photo=unsolicited_photo_id, caption=adm_text, parse_mode="HTML", reply_markup=kb.admin_verify_kb(mat_id))
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")
                
        await callback.answer()
        return

    # Обычный сценарий: сразу просим скинуть фото с названием в одном сообщении
    await state.update_data(
        mat_type=mat_type,
        task_id=task_id,
        subject_id=subject_id
    )
    await state.set_state(Form.waiting_for_material_file)
    
    text = (
        "📸 <b>Скинь фото и название сразу в одном сообщении!</b>\n\n"
        "1. Прикрепи <b>фотографию</b> (тетрадки, экрана или задания) 📷.\n"
        "2. В поле подписи к фото напиши название работы или варианта.\n\n"
        "<i>💡 Можно отправить и без подписи — бот сам назовёт «Фото решения».</i>\n"
        "<i>💡 Или можешь сначала написать название текстом, а следующим сообщением скинуть фото!</i>\n\n"
        "<i>(или отправь /cancel для отмены)</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.back_to_menu_kb())
    await callback.answer()

@dp.message(Form.waiting_for_material_title)
async def process_material_title(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    if message.photo or message.document:
        await process_material_file(message, state, bot)
        return
    if not message.text or not message.text.strip():
        await show_user_screen(message, "⚠️ Отправь название текстом или прикрепи фотографию/файл.", reply_markup=kb.back_to_menu_kb())
        return

    title = message.text.strip()
    await state.update_data(mat_title=title)
    await state.set_state(Form.waiting_for_material_file)
    
    text = (
        f"✍️ Название <b>«{html.escape(title)}»</b> принято!\n\n"
        f"📸 Теперь <b>скинь фотографию</b> (или файл) 📷 👇\n\n"
        f"<i>(или отправь /cancel для отмены)</i>"
    )
    await show_user_screen(message, text, reply_markup=kb.back_to_menu_kb())

@dp.message(Form.waiting_for_material_file)
async def process_material_file(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    data = await state.get_data()
    mat_type = data.get("mat_type", "solution")
    task_id = data.get("task_id")
    subject_id = data.get("subject_id")
    title = data.get("mat_title")
    
    user_id = message.from_user.id
    task_info = await adb.get_task_info_for_subject(user_id, subject_id, task_id) if subject_id else None
    if not task_info:
        await state.clear()
        await show_user_screen(message, "⚠️ Работа больше недоступна. Начни загрузку материала заново.", reply_markup=kb.back_to_menu_kb())
        return

    user_name = message.from_user.username or message.from_user.full_name or f"User_{user_id}"
    if message.from_user.username:
        user_name = f"@{message.from_user.username}"
        
    file_id = None
    file_type = "text"
    content_text = None
    default_title = "Фото методички" if mat_type == "method" else "Фото решения"
    
    # 1. Если прислали фото (с подписью или без)
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        # Если к фото прикреплена подпись — она в приоритете как название!
        if message.caption and message.caption.strip():
            title = message.caption.strip()
        elif not title:
            title = default_title
            
    # 2. Если прислали документ (PDF, Word и т.д.)
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
        content_text = message.document.file_name
        if message.caption and message.caption.strip():
            title = message.caption.strip()
        elif not title:
            title = message.document.file_name or ("Методичка" if mat_type == "method" else "Решение")
            
    # 3. Если прислали текст (название перед фото или текстовое решение)
    elif message.text:
        text_val = message.text.strip()
        if not title:
            # Сохраняем введенный текст как название, просим скинуть фото и даем кнопку сохранить только текстом
            await state.update_data(mat_title=text_val)
            safe_entered_title = html.escape(text_val)
            await show_user_screen(
                message,
                f"✍️ Название <b>«{safe_entered_title}»</b> принято!\n\n"
                f"📸 Теперь просто <b>прикрепи фотографию</b> (или файл) к этому названию 📷 👇\n\n"
                f"<i>(Или нажми кнопку ниже, если хочешь отправить материал только текстом без фото)</i>",
                reply_markup=kb.text_only_or_cancel_kb(task_id, subject_id)
            )
            return
        else:
            file_type = "text"
            content_text = text_val
    else:
        await show_user_screen(message, "⚠️ Пожалуйста, просто скинь фотографию, документ или текст.", reply_markup=kb.back_to_menu_kb())
        return
        
    # Сохраняем в базу со статусом 'pending'
    mat_id = await adb.add_material(
        user_id=user_id,
        user_name=user_name,
        task_id=task_id,
        material_type=mat_type,
        title=title,
        file_id=file_id,
        file_type=file_type,
        content_text=content_text
    )
    if mat_id is None:
        await state.clear()
        await show_user_screen(message, "⚠️ Не удалось сохранить материал: работа не найдена.", reply_markup=kb.back_to_menu_kb())
        return
    await state.clear()
    
    task_info = await adb.get_task_info(task_id)
    safe_sub = html.escape(task_info["subject_name"]) if task_info else ""
    safe_task = html.escape(task_info["title"]) if task_info else ""
    
    # 1. Сообщение пользователю с обратной связью от Менмы
    await show_user_screen(
        message,
        "🚀 <b>Материал успешно отправлен!</b>\n\n"
        "💬 <b>Сообщение от Менмы:</b>\n"
        "<i>«Щас проверю 😎🤙»</i>\n\n"
        "⏳ Твой файл передан на верификацию к @Menmamoh. Как только он нажмёт «Одобрить», материал сразу появится в базе знаний бота у всех студентов!",
        reply_markup=kb.material_sent_success_kb(subject_id)
    )
    
    # 2. Мгновенное уведомление администратору на модерацию
    admin_id = await adb.get_admin_id() or ADMIN_ID
    if admin_id:
        icon = "📖" if mat_type == "method" else "💡"
        type_str = "Методичка" if mat_type == "method" else "Решение"
        
        adm_text = (
            f"🔔 <b>НОВЫЙ МАТЕРИАЛ НА ВЕРИФИКАЦИЮ!</b>\n\n"
            f"👤 <b>От:</b> {html.escape(user_name)} (<code>{user_id}</code>)\n"
            f"📚 <b>Предмет:</b> {safe_sub}\n"
            f"📝 <b>Работа:</b> {safe_task}\n"
            f"🏷️ <b>Тип:</b> {icon} {type_str}\n"
            f"📄 <b>Название:</b> {html.escape(title)}\n"
        )
        if content_text and file_type == "text":
            adm_text += f"\n💬 <b>Содержимое:</b>\n{html.escape(content_text)}"
            
        try:
            if file_type == "document" and file_id:
                await bot.send_document(chat_id=admin_id, document=file_id, caption=adm_text, parse_mode="HTML", reply_markup=kb.admin_verify_kb(mat_id))
            elif file_type == "photo" and file_id:
                await bot.send_photo(chat_id=admin_id, photo=file_id, caption=adm_text, parse_mode="HTML", reply_markup=kb.admin_verify_kb(mat_id))
            else:
                await bot.send_message(chat_id=admin_id, text=adm_text, parse_mode="HTML", reply_markup=kb.admin_verify_kb(mat_id))
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

# --- Если пользователь выбрал сохранить введенный текст без фото ---
@dp.callback_query(F.data.startswith("save_text_mat_"))
async def cb_save_text_mat(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split("_")
    task_id = int(parts[3])
    subject_id = int(parts[4])
    
    data = await state.get_data()
    title = data.get("mat_title") or "Текстовое решение"
    mat_type = data.get("mat_type", "solution")
    
    user_id = callback.from_user.id
    if data.get("task_id") != task_id or data.get("subject_id") != subject_id or mat_type not in {"method", "solution"}:
        await callback.answer("Сеанс загрузки завершён. Начни заново.", show_alert=True)
        return
    task_info = await adb.get_task_info_for_subject(user_id, subject_id, task_id)
    if not task_info:
        await callback.answer("Работа не найдена или уже удалена.", show_alert=True)
        return
    user_name = callback.from_user.username or callback.from_user.full_name or f"User_{user_id}"
    if callback.from_user.username:
        user_name = f"@{callback.from_user.username}"
        
    mat_id = await adb.add_material(
        user_id=user_id,
        user_name=user_name,
        task_id=task_id,
        material_type=mat_type,
        title=title,
        file_id=None,
        file_type="text",
        content_text=title
    )
    if mat_id is None:
        await callback.answer("Не удалось сохранить материал.", show_alert=True)
        return
    await state.clear()
    
    task_info = await adb.get_task_info(task_id)
    safe_sub = html.escape(task_info["subject_name"]) if task_info else ""
    safe_task = html.escape(task_info["title"]) if task_info else ""
    
    # 1. Сообщение пользователю
    await callback.message.edit_text(
        "🚀 <b>Текстовый материал успешно отправлен!</b>\n\n"
        "💬 <b>Сообщение от Менмы:</b>\n"
        "<i>«Щас проверю 😎🤙»</i>\n\n"
        "⏳ Твой материал передан на верификацию к @Menmamoh. Как только он нажмёт «Одобрить», он появится в базе бота!",
        parse_mode="HTML",
        reply_markup=kb.material_sent_success_kb(subject_id)
    )
    
    # 2. Уведомление администратору
    admin_id = await adb.get_admin_id() or ADMIN_ID
    if admin_id:
        icon = "📖" if mat_type == "method" else "💡"
        type_str = "Методичка" if mat_type == "method" else "Решение"
        adm_text = (
            f"🔔 <b>НОВЫЙ МАТЕРИАЛ НА ВЕРИФИКАЦИЮ!</b>\n\n"
            f"👤 <b>От:</b> {html.escape(user_name)} (<code>{user_id}</code>)\n"
            f"📚 <b>Предмет:</b> {safe_sub}\n"
            f"📝 <b>Работа:</b> {safe_task}\n"
            f"🏷️ <b>Тип:</b> {icon} {type_str}\n"
            f"📄 <b>Название:</b> {html.escape(title)}\n\n"
            f"💬 <b>Текст:</b>\n{html.escape(title)}"
        )
        try:
            await bot.send_message(chat_id=admin_id, text=adm_text, parse_mode="HTML", reply_markup=kb.admin_verify_kb(mat_id))
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")
            
    await callback.answer()

# --- Обработка верификации админом ---
@dp.callback_query(F.data.startswith("adm_appr_"))
async def cb_admin_approve(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только @Menmamoh может одобрять материалы!", show_alert=True)
        return
        
    mat_id = int(callback.data.replace("adm_appr_", ""))
    mat = await adb.get_material_by_id(mat_id)
    if not mat:
        await callback.answer("Материал не найден!", show_alert=True)
        return
        
    await adb.set_material_status(mat_id, "approved")
    
    # Редактируем сообщение у админа
    try:
        new_caption = (callback.message.caption or callback.message.text or "") + "\n\n✅ <b>ОДОБРЕНО И ОПУБЛИКОВАНО!</b>"
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(caption=new_caption, parse_mode="HTML", reply_markup=None)
        else:
            await callback.message.edit_text(text=new_caption, parse_mode="HTML", reply_markup=None)
    except Exception:
        pass
        
    await callback.answer("✅ Материал одобрен!")
    
    # Уведомляем автора материала в личку!
    try:
        author_id = mat["user_id"]
        icon = "📖" if mat["material_type"] == "method" else "💡"
        await bot.send_message(
            chat_id=author_id,
            text=f"🎉 <b>Сообщение от Менмы:</b>\n\n"
                 f"Твой материал {icon} <b>«{html.escape(mat['title'])}»</b> проверен и одобрен! 🔥\n"
                 f"Теперь он опубликован в боте и доступен всем студентам. Респект за помощь! 🤝",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not notify author {mat['user_id']}: {e}")

@dp.callback_query(F.data.startswith("adm_rejc_"))
async def cb_admin_reject(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только @Menmamoh может отклонять материалы!", show_alert=True)
        return
        
    mat_id = int(callback.data.replace("adm_rejc_", ""))
    mat = await adb.get_material_by_id(mat_id)
    if not mat:
        await callback.answer("Материал не найден!", show_alert=True)
        return
        
    await adb.set_material_status(mat_id, "rejected")
    
    # Редактируем сообщение у админа
    try:
        new_caption = (callback.message.caption or callback.message.text or "") + "\n\n❌ <b>МАТЕРИАЛ ОТКЛОНЁН.</b>"
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(caption=new_caption, parse_mode="HTML", reply_markup=None)
        else:
            await callback.message.edit_text(text=new_caption, parse_mode="HTML", reply_markup=None)
    except Exception:
        pass
        
    await callback.answer("❌ Материал отклонен.")
    
    # Уведомляем автора
    try:
        author_id = mat["user_id"]
        await bot.send_message(
            chat_id=author_id,
            text=f"❌ <b>Сообщение от Менмы:</b>\n\n"
                 f"Материал <b>«{html.escape(mat['title'])}»</b> был отклонен.\n"
                 f"Возможно файл некорректен или не относится к этой работе. Проверь и загрузи заново!",
            parse_mode="HTML"
        )
    except Exception:
        pass

# --- Если студент просто скинул фото в чат (без кнопок) ---
@dp.message(F.photo)
async def cb_photo_unsolicited(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        # Если бот уже ожидает какое-то другое действие от пользователя
        return
        
    if not await is_tt36_student(message.from_user):
        return

    user_id = message.from_user.id
    u_rec = await adb.get_user(user_id)
    s_grp = u_rec.get("student_group") if u_rec else None
    await adb.seed_default_subjects(user_id, student_group=s_grp)
    subjects = await adb.get_subjects_with_counts(user_id)
    
    file_id = message.photo[-1].file_id
    caption = message.caption.strip() if message.caption else None
    await state.update_data(
        unsolicited_photo_id=file_id,
        unsolicited_caption=caption
    )
    
    text = (
        "📸 <b>Вижу фотографию!</b>\n\n"
        "К какому предмету относится это решение или методичка?\n"
        "Выбери предмет ниже 👇"
    )
    await show_user_screen(message, text, reply_markup=kb.quick_subjects_select_kb(subjects))

# =========================================================================
# --- БАЗОВЫЕ ОПЕРАЦИИ С ПРЕДМЕТАМИ И ЗАДАЧАМИ ---
# =========================================================================

# --- Добавление предмета (FSM) ---
@dp.callback_query(F.data == "add_subject_prompt")
async def cb_add_subject_prompt(callback: CallbackQuery, state: FSMContext):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    await state.set_state(Form.waiting_for_subject_name)
    text = (
        "✍️ <b>Введите название нового предмета</b>\n\n"
        "Например: <code>Инженерная графика</code> или <code>Сопромат</code>.\n\n"
        "<i>(или отправь /cancel для отмены)</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()

@dp.message(Form.waiting_for_subject_name)
async def process_subject_name(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    user_id = message.from_user.id
    name = (message.text or message.caption or "").strip()
    if not name:
        await show_user_screen(message, "⚠️ Название предмета не может быть пустым. Попробуй ещё раз:", reply_markup=kb.back_to_menu_kb())
        return
    
    sub_id = await adb.add_subject(user_id, name)
    await state.clear()
    
    safe_name = html.escape(name)
    if sub_id:
        text = f"✅ Предмет <b>«{safe_name}»</b> успешно добавлен!"
    else:
        text = f"⚠️ Предмет <b>«{safe_name}»</b> уже есть в твоем списке!"
        
    subjects = await adb.get_subjects_with_counts(user_id)
    await show_user_screen(message, text, reply_markup=kb.subjects_list_kb(subjects))

# --- Добавление работы в предмет (FSM) ---
@dp.callback_query(F.data.startswith("add_task_prompt_"))
async def cb_add_task_prompt(callback: CallbackQuery, state: FSMContext):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    subject_id = int(callback.data.replace("add_task_prompt_", ""))
    subject = await adb.get_subject_by_id(callback.from_user.id, subject_id)
    if not subject:
        await callback.answer("Предмет не найден или уже удалён.", show_alert=True)
        return
    await state.update_data(current_subject_id=subject_id)
    await state.set_state(Form.waiting_for_task_title)
    safe_name = html.escape(subject["name"])
    text = (
        f"✍️ <b>Добавление работы в «{safe_name}»</b>\n\n"
        f"Напиши название работы (можно несколько через запятую или каждую с новой строки).\n\n"
        f"<b>Пример:</b>\n"
        f"<code>Лабораторная работа №1</code>\n"
        f"<code>Расчётно-графическая работа</code>\n"
        f"<code>Курсовой проект</code>\n\n"
        f"<i>(или отправь /cancel для отмены)</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()

@dp.message(Form.waiting_for_task_title)
async def process_task_title(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    data = await state.get_data()
    subject_id = data.get("current_subject_id")
    user_id = message.from_user.id
    subject = await adb.get_subject_by_id(user_id, subject_id) if subject_id else None
    if not subject:
        await state.clear()
        await show_user_screen(message, "⚠️ Предмет не найден или уже удалён. Начни добавление работы заново.", reply_markup=kb.back_to_menu_kb())
        return
    
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text:
        await show_user_screen(message, "⚠️ Отправь название работы текстом. Можно указать несколько строк.", reply_markup=kb.back_to_menu_kb())
        return
    lines = [line.strip() for line in raw_text.replace(",", "\n").split("\n") if line.strip()]
    added_count = await adb.add_tasks(user_id, subject_id, lines)
            
    await state.clear()
    
    page_size = 25
    subject_page = await adb.get_subject_tasks_page(user_id, subject_id, page_size, 0)
    tasks = subject_page["tasks"]
    total_tasks = subject_page["total"]
    pending_count = subject_page["pending"]
    safe_name = html.escape(subject["name"])
    mat_counts = await adb.get_material_counts_for_tasks([task["id"] for task in tasks])
    
    text = (
        f"🎉 Добавлено работ: <b>{added_count}</b>!\n\n"
        f"📖 <b>Предмет:</b> {safe_name}\n"
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (долгов: <b>{pending_count}</b>)\n\n"
        f"• Нажатие <b>слева</b> переключает статус сдачи\n"
        f"• Кнопка <b>справа 📚</b> открывает проверенные методички и решения"
    )
    await show_user_screen(
        message,
        text, parse_mode="HTML",
        reply_markup=kb.subject_tasks_kb(subject_id, tasks, mat_counts, 0, total_tasks, page_size)
    )

# --- Удаление предмета ---
@dp.callback_query(F.data.startswith("confirm_del_sub_"))
async def cb_confirm_del_sub(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    subject_id = int(callback.data.replace("confirm_del_sub_", ""))
    user_id = callback.from_user.id
    subject = await adb.get_subject_by_id(user_id, subject_id)
    if not subject:
        await callback.answer("Предмет не найден или уже удалён.", show_alert=True)
        return
    safe_name = html.escape(subject["name"])
    
    text = f"❓ Ты точно хочешь удалить предмет <b>«{safe_name}»</b> и ВСЕ его работы?"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.confirm_delete_subject_kb(subject_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("del_sub_"))
async def cb_del_sub(callback: CallbackQuery):
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    subject_id = int(callback.data.replace("del_sub_", ""))
    user_id = callback.from_user.id
    if not await adb.get_subject_by_id(user_id, subject_id):
        await callback.answer("Предмет уже удалён.", show_alert=True)
        return
    await adb.delete_subject(user_id, subject_id)
    
    subjects = await adb.get_subjects_with_counts(user_id)
    text = "🗑️ Предмет успешно удален!"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.subjects_list_kb(subjects))
    await callback.answer()

async def gdrive_sync_background_loop(bot: Bot):
    """Фоновый опрос Google Диска: проверяет новые файлы каждые 30 минут."""
    await asyncio.sleep(15)
    while True:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, gdrive_sync.sync_drive_schedule)
            if res.get("ok") and res.get("new_downloaded", 0) > 0:
                logger.info(f"Google Drive background sync: downloaded {res['new_downloaded']} new files")
                if ADMIN_ID:
                    try:
                        new_files_str = "\n".join([f"• {f['filename']}" for f in res.get("updated_files", [])])
                        await bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                f"📢 <b>Google Диск: обнаружены новые замены!</b>\n\n"
                                f"Скачано новых файлов: <b>{res['new_downloaded']}</b>\n"
                                f"{new_files_str}\n\n"
                                f"<i>Листы замен в боте обновлены автоматически.</i>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.warning(f"Could not notify admin of gdrive sync: {e}")
        except Exception as e:
            logger.error(f"Error in gdrive sync loop: {e}")

        await asyncio.sleep(1800)

# --- Точка входа ---
async def main():
    global ADMIN_ID
    await adb.init_db()
    await adb.seed_default_group_schedule()
    if ADMIN_ID is None:
        ADMIN_ID = await adb.get_admin_id()
    if ADMIN_ID is None:
        logger.error("No administrator ID configured; set ADMIN_ID in .env before starting the bot")
        raise SystemExit(1)
    
    token = BOT_TOKEN
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n" + "="*60)
        print("⚠️ ОШИБКА: Не указан токен Telegram-бота!")
        print("="*60 + "\n")
        sys.exit(1)
        
    bot = Bot(token=token)
    try:
        await cleanup_legacy_user_menus(bot)
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="🚀 Начать / Главное меню"),
            BotCommand(command="verify", description="🎓 Выбрать группу"),
            BotCommand(command="schedule", description="📅 Расписание занятий"),
            BotCommand(command="bells", description="🔔 Расписание звонков"),
            BotCommand(command="my_id", description="🆔 Мой ID")
        ])
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    # Запуск фоновой синхронизации с Google Диском
    asyncio.create_task(gdrive_sync_background_loop(bot))

    print("🚀 Бот «Помощник Пятнашки» запущен и слушает Telegram...")
    await dp.start_polling(bot, drop_pending_updates=False)

if __name__ == "__main__":
    asyncio.run(main())

