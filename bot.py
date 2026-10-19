Warning: truncated output (original token count: 43470)
Total output lines: 3505

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


async def cleanup_user_ui_messages(
    bot: Bot,
    user_id: int,
    keep_message_id: Optional[int] = None,
    extra_ids=(),
    keep_message_ids=(),
):
    """Deletes all tracked duplicate menu messages except the one being shown."""
    tracked_ids = set(await adb.get_user_ui_message_ids(user_id))
    tracked_ids.update(int(message_id) for message_id in extra_ids if message_id)
    keep_ids = {int(message_id) for message_id in keep_message_ids if message_id}
    if keep_message_id:
        keep_ids.add(int(keep_message_id))
    for message_id in sorted(tracked_ids):
        if message_id in keep_ids:
            continue
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.delete_message(chat_id=user_id, message_id=message_id)
            except Exception as retry_error:
                logger.debug(
                    "Could not remove duplicate UI message %s for user %s: %s",
                    message_id,
                    user_id,
                    retry_error,
                )
                continue
        except Exception as error:
            logger.debug(
                "Could not remove duplicate UI message %s for user %s: %s",
                message_id,
                user_id,
                error,
            )
            continue
        await adb.forget_ui_message_id(user_id, message_id)

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
MAIN_MENU_PHOTO_PATH = os.path.join(os.path.dirname(__file__), "assets", "main_menu_meme.jpg")
APPLICATION_EXAMPLE_PHOTO = os.path.join(os.path.dirname(__file__), "assets", "useful", "application_example.jpg")
APPLICATION_TEMPLATE_PDF = os.path.join(os.path.dirname(__file__), "assets", "useful", "application_template.pdf")
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
    """Prefers the latest shared Drive sheets, then a manually uploaded shared photo."""
    if target_date is None:
        target_date, _, _ = gdrive_sync.get_target_study_date()

    manual_is_newer = manual_general_schedule_is_newer(gen_sched, target_date)
    if manual_is_newer:
        if gen_sched.get("photo_file_id"):
            return gen_sched["photo_file_id"]
        return None

    drive_photos = gdrive_sync.get_active_gdrive_schedule_photos(target_date=target_date)
    available_drive_paths = [
        path for path, _, valid_until in drive_photos
        if valid_until >= target_date and os.path.isfile(path)
    ]
    if len(available_drive_paths) >= 2:
        return available_drive_paths
    if available_drive_paths:
        path = available_drive_paths[0]
        drive_file = db.get_gdrive_file_by_local_path(path)
        if drive_file:
            cached_file_id = db.get_cached_telegram_schedule_file(
                drive_file["file_id"], drive_file.get("drive_timestamp") or 0
            )
            if cached_file_id:
                return cached_file_id
        return path
    if gen_sched and gen_sched.get("photo_file_id"):
        return gen_sched["photo_file_id"]
    fallback_paths = [
        path
        for path, _, valid_until in GENERAL_SCHEDULE_PHOTOS
        if valid_until >= target_date and os.path.isfile(path)
    ]
    if len(fallback_paths) >= 2:
        return fallback_paths
    if fallback_paths:
        return fallback_paths[0]
    return None


def manual_general_schedule_is_newer(gen_sched: Optional[Dict], target_date: datetime.date) -> bool:
    """True when a manual ALL schedule was sent after the active Drive file changed."""
    if not gen_sched or not (gen_sched.get("schedule_text") or gen_sched.get("photo_file_id")):
        return False
    try:
        manual_time = datetime.datetime.fromisoformat(str(gen_sched.get("updated_at")))
        if manual_time.tzinfo is None:
            manual_time = manual_time.replace(tzinfo=datetime.timezone.utc)
        manual_timestamp = manual_time.timestamp()
    except (TypeError, ValueError):
        return False

    drive_files = []
    for doc_type in ("replacement", "consultations"):
        drive_files.extend(db.get_latest_gdrive_schedule_files(
            doc_type=doc_type, min_date=target_date.isoformat()
        ))
    drive_timestamps = [int(file.get("drive_timestamp") or 0) for file in drive_files]
    drive_timestamps = [value / 1000 if value > 10**12 else value for value in drive_timestamps if value]
    if not drive_timestamps:
        return True
    return manual_timestamp > max(drive_timestamps)


def build_general_schedule_media(photo_paths: List[str], target_date: Optional[datetime.date] = None) -> List[InputMediaPhoto]:
    """Готовит исходные фото общего листа для отправки альбомом."""
    photos_pool = get_current_general_schedule_photos(target_date=target_date)
    captions_by_path = {path: caption for path, caption, _ in photos_pool}
    media = []
    for path in photo_paths:
        drive_file = db.get_gdrive_file_by_local_path(path)
        telegram_file_id = (
            db.get_cached_telegram_schedule_file(
                drive_file["file_id"], drive_file.get("drive_timestamp") or 0
            )
            if drive_file else None
        )
        media.append(InputMediaPhoto(
            media=telegram_file_id or FSInputFile(path),
            caption=captions_by_path.get(path),
            parse_mode="HTML",
        ))
    return media


async def cache_general_schedule_media(photo_paths: List[str], sent_messages):
    """Stores Telegram file IDs so shared Drive images are uploaded only once."""
    for path, sent in zip(photo_paths, sent_messages):
        if not getattr(sent, "photo", None):
            continue
        drive_file = await adb.get_gdrive_file_by_local_path(path)
        if not drive_file:
            continue
        await adb.save_cached_telegram_schedule_file(
            drive_file["file_id"],
            sent.photo[-1].file_id,
            drive_file.get("drive_timestamp") or 0,
        )


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

    if cb and msg and getattr(msg.chat, "type", None) == "private":
        saved_id = await adb.get_ui_message_id(user_id)
        if saved_id and saved_id != msg.message_id:
            try:
                sent = await bot_inst.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                await adb.set_ui_message_id(user_id, sent.message_id)
                await cleanup_user_ui_messages(bot_inst, user_id, keep_message_id=sent.message_id, extra_ids=(msg.message_id,))
                return
            except Exception as error:
                logger.warning("Could not replace stale UI message for user %s: %s", user_id, error)

    # Если исходное сообщение содержало фото — редактируем подпись к нему
    if getattr(msg, "photo", None) and hasattr(msg, "edit_caption"):
        try:
            await msg.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            if cb and getattr(msg.chat, "type", None) == "private":
                await adb.set_ui_message_id(cb.from_user.id, msg.message_id)
                await cleanup_user_ui_messages(bot_inst, cb.from_user.id, keep_message_id=msg.message_id)
            return
        except Exception:
            pass

    # Если текстовое сообщение или edit_caption не сработал — пробуем edit_text
    if hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            if cb and getattr(msg.chat, "type", None) == "private":
                await adb.set_ui_message_id(cb.from_user.id, msg.message_id)
                await cleanup_user_ui_messages(bot_inst, cb.from_user.id, keep_message_id=msg.message_id)
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
            if cb and getattr(msg.chat, "type", None) == "private":
                await cleanup_user_ui_messages(bot_inst, user_id, keep_message_id=sent.message_id, extra_ids=(msg.message_id,))
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
        if photo:
            sent = await callback.bot.send_photo(
                chat_id=user_id,
                photo=FSInputFile(photo) if os.path.isfile(photo) else photo,
                caption=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            await adb.set_ui_message_id(user_id, sent.message_id)
            await cleanup_user_ui_messages(
                callback.bot,
                user_id,
                keep_message_id=sent.message_id,
                extra_ids=(message.message_id,),
            )
            return
        saved_id = await adb.get_ui_message_id(user_id)
        if saved_id and saved_id != message.message_id:
            sent = await callback.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            await adb.set_ui_message_id(user_id, sent.message_id)
            await cleanup_user_ui_messages(
                callback.bot,
                user_id,
                keep_message_id=sent.message_id,
                extra_ids=(message.message_id,),
            )
            return
        try:
            if getattr(message, "photo", None):
                await message.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            else:
                await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            await adb.set_ui_message_id(user_id, message.message_id)
            await cleanup_user_ui_messages(callback.bot, user_id, keep_message_id=message.message_id)
            return
        except Exception as error:
            if "message is not modified" in str(error).lower():
                await adb.set_ui_message_id(user_id, message.message_id)
                await cleanup_user_ui_messages(callback.bot, user_id, keep_message_id=message.message_id)
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
        await cleanup_user_ui_messages(
            callback.bot,
            user_id,
            keep_message_id=sent.message_id,
            extra_ids=(message.message_id,),
        )
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
    await cleanup_user_ui_messages(bot_inst, user_id, keep_message_id=sent.message_id)


async def render_admin_verification_queue(bot: Bot, page: int = 0, recreate: bool = False):
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
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ])

    saved_id = await adb.get_verification_ui_message_id(admin_id)
    if saved_id and recreate:
        # When the admin explicitly opens/refreshes the queue, move it to the
        # bottom of the chat instead of editing an old message far above.
        try:
            await bot.delete_message(chat_id=admin_id, message_id=saved_id)
        except Exception:
            pass
        await adb.clear_verification_ui_message_id(admin_id)
        saved_id = None
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
            await adb.forget_ui_message_id(user_id, message_id)
            deleted += 1
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.delete_message(chat_id=user_id, message_id=message_id)
                await adb.forget_ui_message_id(user_id, message_id)
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
    # Preserve the administrator's established default schedule even if an older
    # database snapshot has an approved admin row with an empty group field.
    if ADMIN_ID and user_id == ADMIN_ID:
        return "ТТ-36"
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
                "useful_menu", "useful_application", "notification_open",
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
        "Отправь текст или фотографию с подписью. Перед публикацией покажу предпросмотр и попрошу подтвердить.\n\n"
        "После отправки у подтверждённых пользователей появится пункт «Уведомление» в главном меню. Для отмены нажми «Главное меню»."
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
    await callback.message.edit_text("📢 <b>Публикую объявление в разделе уведомлений…</b>", parse_mode="HTML", reply_markup=None)
    await adb.publish_announcement(source_chat_id, source_message_id)
    users = await adb.get_verified_users()
    recipient_count = sum(1 for recipient in users if recipient["user_id"] != callback.from_user.id)

    report = (
        "✅ <b>Объявление опубликовано</b>\n\n"
        f"Уведомление появится в главном меню у <b>{recipient_count}</b> подтверждённых пользователей.\n"
        "Оно будет отображаться, пока пользователь его не откроет."
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
    has_unread_notification = bool(await adb.get_unread_announcement(user_id)) if is_verified else False

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
        is_p…18470 tokens truncated…тся список предметов для быстрого удаления устаревших файлов."
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
    logger.info(
        "SQLite database: %s (verified users: %s)",
        db.DB_PATH,
        await adb.count_verified_users(),
    )
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
        ])
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    # Запуск фоновой синхронизации с Google Диском
    asyncio.create_task(gdrive_sync_background_loop(bot))

    print("🚀 Бот «Помощник Пятнашки» запущен и слушает Telegram...")
    await dp.start_polling(bot, drop_pending_updates=False)

if __name__ == "__main__":
    asyncio.run(main())

