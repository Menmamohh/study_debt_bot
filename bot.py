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
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

import database as db
import keyboards as kb
import gdrive_sync
import schedule_review
import schedule_review as gemini_schedule


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
            return gen_sched.get("photo_file_ids") or gen_sched["photo_file_id"]
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
        return gen_sched.get("photo_file_ids") or gen_sched["photo_file_id"]
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
    waiting_for_schedule_review = State()
    waiting_for_group_schedule = State()
    waiting_for_custom_group = State()
    waiting_for_group_confirmation = State()
    waiting_for_student_name = State()
    waiting_for_feedback = State()
    waiting_for_feedback_reply = State()
    waiting_for_feedback_reply_preview = State()
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
            photo=FSInputFile(photo) if isinstance(photo, str) and os.path.isfile(photo) else photo,
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
        state = data.get('state')
        if user and state:
            state_data = await state.get_data()
            if state_data.get('feedback_reply_token'):
                leaving = (isinstance(event, CallbackQuery) and not (event.data or '').startswith('fbr_'))
                leaving = leaving or (isinstance(event, Message) and bool(event.text) and
                    (event.text.startswith('/') or event.text.strip().lower() in {'начать', '🚀 начать', 'старт', 'start', 'меню', 'главное меню'}))
                if leaving:
                    await adb.cancel_feedback_reply_drafts(user.id)
                    await state.clear()
        if isinstance(event, CallbackQuery) and event.message and event.message.chat.type != "private":
            callback_data = event.data or ""
            private_prefixes = (
                "open_sub_", "toggle_t_", "view_mat_", "dl_mat_", "add_task_prompt_",
                "confirm_del_sub_", "del_sub_", "b_sub_", "quick_", "upload_mat_",
                "set_mat_type_", "save_text_mat_", "show_all_debts_", "adm_", "important_close_", "sr_",
            )
            private_callbacks = {
                "main_menu", "debts_hub", "show_all_debts", "list_subjects",
                "add_subject_prompt", "browse_materials_hub", "start_verify_flow", "feedback_start",
                "useful_menu", "useful_application", "notification_open", "profile_menu",
                "profile_change_group",
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


class ScheduleAlbumMiddleware(BaseMiddleware):
    """Telegram delivers each album photo as an update; publish once after collecting it."""

    def __init__(self, settle_seconds=1.0):
        self.pending = {}
        self.settle_seconds = settle_seconds

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if (not event.media_group_id or not state or not is_admin(event.from_user)
                or event.chat.type != "private"
                or await state.get_state() != Form.waiting_for_group_schedule.state):
            return await handler(event, data)
        key = (event.chat.id, event.from_user.id, event.media_group_id)
        if key in self.pending:
            self.pending[key][event.message_id] = event
            return
        album = self.pending[key] = {event.message_id: event}
        try:
            while True:
                count = len(album)
                await asyncio.sleep(self.settle_seconds)
                if count == len(album):
                    break
            if await state.get_state() != Form.waiting_for_group_schedule.state:
                return
            data["schedule_album"] = [album[item] for item in sorted(album)]
            return await handler(data["schedule_album"][0], data)
        finally:
            self.pending.pop(key, None)


dp.message.middleware(ScheduleAlbumMiddleware())

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
    
    pending_mat_count, pending_users_count, pending_feedback_count = await asyncio.gather(
        adb.count_pending_materials(),
        adb.count_pending_users(),
        adb.count_open_feedback_reports(),
    )
    
    markup = kb.admin_panel_kb(
        pending_materials_count=pending_mat_count,
        pending_users_count=pending_users_count,
        pending_feedback_count=pending_feedback_count,
    )
    
    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="📝 Черновики замен", callback_data="sr_queue")])

    status_lines = []
    if pending_users_count > 0:
        status_lines.append(f"👥 <b>Заявок на вход (верификация):</b> <b>{pending_users_count}</b> шт.")
    if pending_mat_count > 0:
        status_lines.append(f"📋 <b>Материалов на проверке:</b> <b>{pending_mat_count}</b> шт.")
    if pending_feedback_count > 0:
        status_lines.append(f"🐞 <b>Новых отчётов об ошибках:</b> <b>{pending_feedback_count}</b> шт.")
        
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


@dp.callback_query(F.data == "adm_test_mode")
async def cb_admin_test_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Режим доступен только администратору в личном чате.", show_alert=True)
        return
    text = (
        "🧪 <b>Тестовый просмотр групп</b>\n\n"
        "Выбери группу, чтобы посмотреть её предметы, число опубликованных материалов и расписание. "
        "Личные долги и прогресс студентов не отображаются и не изменяются."
    )
    await show_user_screen(callback, text, reply_markup=kb.admin_test_groups_kb(db.COLLEGE_GROUPS))
    await callback.answer()


@dp.callback_query(F.data == "adm_ocr_mode")
async def cb_admin_ocr_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Проверка доступна только администратору в личном чате.", show_alert=True)
        return
    text = (
        "🔎 <b>Проверка распознавания расписания</b>\n\n"
        "Выбери группу. Бот на сервере прочитает самый свежий сохранённый лист замен, покажет его дату, найденный текст и исходные фото. "
        "Этот результат увидишь только ты; студентам он не отправляется."
    )
    await show_user_screen(callback, text, reply_markup=kb.admin_ocr_groups_kb(db.COLLEGE_GROUPS))
    await callback.answer()


async def show_gemini_schedule_check(callback, files, group_name):
    try:
        await show_user_screen(callback, "⏳ Проверяю доступ к Gemini без отправки фото…",
                               reply_markup=kb.admin_ocr_result_kb())
        model = await asyncio.to_thread(gemini_schedule.check_connection)
        await show_user_screen(callback,
                               f"✅ Доступ к {html.escape(model)} подтверждён. Распознаю фото…",
                               reply_markup=kb.admin_ocr_result_kb())
        result = await asyncio.to_thread(
            gemini_schedule.read_schedule, [item["local_path"] for item in files], group_name
        )
    except gemini_schedule.GeminiError as error:
        await show_user_screen(callback, "⚠️ " + html.escape(str(error)), reply_markup=kb.admin_ocr_result_kb())
        return
    except OSError:
        await show_user_screen(callback, "⚠️ Не удалось открыть исходные фото. Обнови файлы с Google Диска.",
                               reply_markup=kb.admin_ocr_result_kb())
        return
    date = files[0].get("schedule_date") or "дата не указана"
    await show_user_screen(
        callback,
        f"📅 <b>Замены · {html.escape(group_name)} · {html.escape(date)}</b>\n\n"
        + html.escape(result["text"])
        + f"\n\n<i>Проверка Gemini · {html.escape(result['model'])}. Сверь с фото. Студентам не публикуется.</i>",
        reply_markup=kb.admin_ocr_result_kb(),
    )
    for item in files:
        try:
            sent = await callback.bot.send_photo(chat_id=callback.from_user.id,
                photo=FSInputFile(item["local_path"]), caption="Исходный лист для проверки Gemini")
            await adb.set_ui_message_id(callback.from_user.id, sent.message_id)
        except Exception:
            logger.warning("Could not attach Gemini schedule source photo")


@dp.callback_query(F.data.startswith("adm_ocr_group_"))
async def cb_admin_ocr_group(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Проверка доступна только администратору в личном чате.", show_alert=True)
        return
    group_name = callback.data.removeprefix("adm_ocr_group_")
    if group_name not in db.COLLEGE_GROUPS:
        await callback.answer("Неизвестная группа.", show_alert=True)
        return

    await callback.answer("Распознаю лист на сервере. Это может занять до минуты на каждую страницу.")
    await show_user_screen(
        callback,
        f"⏳ Проверяю распознавание для <b>{html.escape(group_name)}</b> на сервере…",
        reply_markup=kb.admin_ocr_result_kb(),
    )
    files = await asyncio.to_thread(
        db.get_latest_gdrive_schedule_files,
        doc_type="replacement",
    )
    files = [item for item in files if item.get("local_path") and os.path.isfile(item["local_path"])]
    if not files:
        await show_user_screen(
            callback,
            "📄 Не нашёл сохранённые листы замен. Сначала обнови расписание с Google Диска.",
            reply_markup=kb.admin_ocr_result_kb(),
        )
        return

    if gemini_schedule.configured():
        await show_gemini_schedule_check(callback, files, group_name)
        return

    pages = []
    for item in files:
        path = item["local_path"]
        modified_at = os.path.getmtime(path)
        width, height, markers = await asyncio.to_thread(
            gdrive_sync._schedule_group_markers, path, modified_at
        )
        text = await asyncio.to_thread(gdrive_sync._ocr_schedule_group, path, modified_at, group_name)
        detected = ", ".join(name for name, _ in markers) or "группы не найдены"
        if not width or not height:
            recognized = "⚠️ Не удалось обработать страницу: проверь серверный журнал и попробуй ещё раз."
        elif group_name not in {name for name, _ in markers}:
            recognized = "⚠️ Эта группа не найдена в распознанных заголовках страницы."
        elif text:
            recognized = text
        else:
            recognized = "⚠️ Заголовок группы найден, но текст расписания прочитать не удалось."
        pages.append((path, item, width, height, detected, recognized))

    target = files[0].get("schedule_date") or "дата не указана"
    matching_pages = [page for page in pages if group_name in page[4].split(", ")]
    if matching_pages:
        pages = matching_pages
    lines = [f"📅 <b>Замены · {html.escape(group_name)} · {html.escape(target)}</b>", ""]
    for _, _, width, height, _, recognized in pages:
        if recognized.startswith("⚠️"):
            lines.append("⚠️ Не удалось собрать строки уроков. Сверь исходное фото.")
        else:
            lines.append(html.escape(recognized[:2800]))
    lines.extend(["", "<i>Проверка для администратора. Сверь текст с фото перед публикацией.</i>"])
    result = "\n".join(lines)
    await show_user_screen(callback, result, reply_markup=kb.admin_ocr_result_kb())

    # Показываем оригиналы после результата и учитываем их в общей уборке
    # сообщений, чтобы они исчезали при следующем переходе по меню.
    for start in range(0, len(pages), 10):
        batch = pages[start:start + 10]
        try:
            if len(batch) == 1:
                sent = [await callback.bot.send_photo(
                    chat_id=callback.from_user.id,
                    photo=FSInputFile(batch[0][0]),
                    caption=f"Исходный лист замен · {batch[0][1].get('schedule_date') or target}",
                )]
            else:
                sent = await callback.bot.send_media_group(
                    chat_id=callback.from_user.id,
                    media=[InputMediaPhoto(
                        media=FSInputFile(path),
                        caption=f"Исходные листы замен · {target}" if offset == 0 else None,
                    ) for offset, (path, *_rest) in enumerate(batch)],
                )
            for photo_message in sent:
                await adb.set_ui_message_id(callback.from_user.id, photo_message.message_id)
        except Exception as error:
            logger.warning("Could not send source images for OCR check: %s", error)
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text="Не получилось прикрепить исходное фото. Распознанный текст и список найденных групп показаны выше.",
            )


@dp.callback_query(F.data.startswith("adm_test_preview_"))
async def cb_admin_test_preview(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Режим доступен только администратору в личном чате.", show_alert=True)
        return
    group_name = callback.data.removeprefix("adm_test_preview_")
    if group_name not in db.COLLEGE_GROUPS:
        await callback.answer("Неизвестная группа.", show_alert=True)
        return
    names, counts = await asyncio.gather(
        adb.get_preview_subject_names_for_group(group_name),
        adb.get_global_material_counts_by_subject(),
    )
    lines = [f"🧪 <b>Тестовый просмотр: {html.escape(group_name)}</b>", ""]
    if not await adb.group_has_subjects_support(group_name):
        lines.append("ℹ️ Для этой группы трекер предметов пока не настроен.")
    if names:
        lines.append("📚 <b>Предметы и опубликованные материалы:</b>")
        lines.extend(
            f"• {html.escape(name)} — {int(counts.get(name.strip().lower(), 0))} шт."
            for name in names
        )
    else:
        lines.append("📚 Для этой группы пока нет настроенных предметов.")
    lines.append("\nЭто только просмотр: данные студентов не меняются.")
    await show_user_screen(
        callback, "\n".join(lines), reply_markup=kb.admin_test_preview_kb(group_name)
    )
    await callback.answer()


async def _show_feedback_queue(callback: CallbackQuery, page: int = 0):
    total = await adb.count_open_feedback_reports()
    reports = await adb.get_open_feedback_reports(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
    if not reports:
        text = "🐞 <b>Новых отчётов об ошибках нет.</b>"
        markup = kb.admin_panel_kb(
            await adb.count_pending_materials(), await adb.count_pending_users(), total
        )
    else:
        text = f"🐞 <b>Отчёты об ошибках: {total}</b> · страница {page + 1}\n\nВыбери отчёт для просмотра."
        markup = kb.admin_feedback_queue_kb(reports, page, total, ADMIN_PAGE_SIZE)
    await show_user_screen(callback, text, reply_markup=markup)


@dp.callback_query(F.data == "adm_feedback_queue")
@dp.callback_query(F.data.startswith("adm_feedback_queue_"))
async def cb_admin_feedback_queue(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Очередь доступна только администратору в личном чате.", show_alert=True)
        return
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "adm_feedback_queue" else 0
    await _show_feedback_queue(callback, page)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_feedback_copy_"))
async def cb_admin_feedback_copy(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Доступ только администратору.", show_alert=True)
        return
    try:
        report_id = int(callback.data.rsplit("_", 1)[1])
        report = await adb.get_feedback_report(report_id)
        if not report:
            await callback.answer("Отчёт не найден.", show_alert=True)
            return
        await bot.copy_message(
            chat_id=callback.from_user.id,
            from_chat_id=report["source_chat_id"],
            message_id=report["source_message_id"],
        )
        await callback.answer("Исходное сообщение отправлено ниже.")
    except Exception:
        logger.exception("Could not copy feedback report")
        await callback.answer("Не удалось открыть исходное сообщение.", show_alert=True)


@dp.callback_query(F.data.startswith("adm_feedback_resolve_"))
async def cb_admin_feedback_resolve(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Доступ только администратору.", show_alert=True)
        return
    report_id = int(callback.data.rsplit("_", 1)[1])
    await adb.resolve_feedback_report(report_id)
    await _show_feedback_queue(callback)
    await callback.answer("Отчёт закрыт.")


@dp.callback_query(F.data.regexp(r"^adm_feedback_\d+$"))
async def cb_admin_feedback_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Доступ только администратору.", show_alert=True)
        return
    report_id = int(callback.data.rsplit("_", 1)[1])
    report = await adb.get_feedback_report(report_id)
    if not report or report.get("resolved_at"):
        await callback.answer("Открытый отчёт не найден.", show_alert=True)
        return
    await show_feedback_detail(callback, report_id)


async def show_feedback_detail(target, report_id: int, page: int = 0, notice: str = ''):
    report = await adb.get_feedback_report(report_id)
    if not report:
        await show_user_screen(target, 'Отчёт не найден.', reply_markup=kb.back_to_menu_kb())
        return
    history = await adb.get_feedback_reply_history(report_id, page)
    esc = lambda value, size: html.escape(str(value or '')[:size])
    text = (f"🐞 <b>Отчёт об ошибке №{report_id}</b>\n\n"
            f"👤 {esc(report.get('user_label') or report['user_id'], 100)} · ID <code>{report['user_id']}</code>\n"
            f"🎓 {esc(report.get('student_group') or 'не выбрана', 40)}\n\n"
            f"{esc(report.get('body_text') or 'Вложение без текста', 800)}")
    if notice:
        text = html.escape(notice) + '\n\n' + text
    text += f"\n\n<b>Отправленные ответы: {history['total']}</b>"
    for reply in history['replies']:
        date = datetime.datetime.fromisoformat(reply['sent_at']).replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo('Europe/Minsk'))
        text += f"\n\n{date:%d.%m.%Y %H:%M} (Минск)\n{esc(reply['body'], 600)}"
        if len(reply['body']) > 600:
            text += '…'
    markup = kb.admin_feedback_item_kb(report_id)
    controls = kb._page_controls(f'fbh_{report_id}', history['page'], history['total'], 3)
    if controls:
        markup.inline_keyboard.insert(0, controls)
    await show_user_screen(target, text, reply_markup=markup)


@dp.callback_query(F.data.regexp(r'^fbh_\d+_\d+$'))
async def cb_feedback_history(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != 'private':
        await callback.answer('Доступ только администратору.', show_alert=True)
        return
    _, report_id, page = callback.data.split('_')
    await show_feedback_detail(callback, int(report_id), int(page))


@dp.callback_query(F.data.regexp(r'^fbr_start_\d+$'))
async def cb_feedback_reply_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user) or callback.message.chat.type != 'private':
        await callback.answer('Доступ только администратору.', show_alert=True)
        return
    await state.clear()
    reply = await adb.create_feedback_reply(int(callback.data.rsplit('_', 1)[1]), callback.from_user.id)
    if not reply:
        await callback.answer('Открытый отчёт не найден.', show_alert=True)
        return
    await state.update_data(feedback_reply_token=reply['token'])
    await state.set_state(Form.waiting_for_feedback_reply)
    report = await adb.get_feedback_report(reply['report_id'])
    label = html.escape(str(report.get('user_label') or reply['recipient_id'])[:100])
    await safe_edit_message(callback, f"💬 Ответ на обращение №{reply['report_id']}\nПолучатель: {label}\n\nВведи текст ответа (до 3500 символов).",
                            reply_markup=kb.feedback_reply_kb(reply['token']))


@dp.message(Form.waiting_for_feedback_reply)
@dp.message(Form.waiting_for_feedback_reply_preview)
async def process_feedback_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user) or message.chat.type != 'private':
        return
    if message.text and (message.text.startswith('/') or message.text.strip().lower() in {'начать', '🚀 начать', 'старт', 'start', 'меню', 'главное меню'}):
        await adb.cancel_feedback_reply_drafts(message.from_user.id)
        await state.clear()
        await cmd_start(message, state)
        return
    if (await state.get_state()) != Form.waiting_for_feedback_reply.state:
        await show_user_screen(message, 'Для изменения ответа нажми «Изменить» в предпросмотре.',
                               reply_markup=kb.feedback_reply_kb((await state.get_data()).get('feedback_reply_token', ''), True))
        return
    body = (message.text or '').strip()
    data = await state.get_data()
    token = data.get('feedback_reply_token', '')
    reply = await adb.get_feedback_reply(token, message.from_user.id)
    report = await adb.get_feedback_report(reply['report_id']) if reply else None
    if not reply or reply['status'] != 'draft' or not report or report.get('resolved_at'):
        await state.clear()
        await show_user_screen(message, 'Черновик устарел. Открой отчёт заново.', reply_markup=kb.back_to_menu_kb())
        return
    if not body or len(body) > 3500 or body.startswith('/'):
        await show_user_screen(message, 'Отправь текст от 1 до 3500 символов.', reply_markup=kb.feedback_reply_kb(token))
        return
    if not await adb.prepare_feedback_reply(token, message.from_user.id, body):
        return
    await state.set_state(Form.waiting_for_feedback_reply_preview)
    label = html.escape(str(report.get('user_label') or reply['recipient_id'])[:100])
    await show_user_screen(message, f"<b>Предпросмотр ответа №{reply['report_id']}</b>\nПолучатель: {label} · ID {reply['recipient_id']}\n\n{html.escape(body)}",
                           reply_markup=kb.feedback_reply_kb(token, True))


@dp.callback_query(F.data.regexp(r'^fbr_(send|edit|cancel)_[a-f0-9]{32}$'))
async def cb_feedback_reply_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user) or callback.message.chat.type != 'private':
        await callback.answer('Доступ только администратору.', show_alert=True)
        return
    _, action, token = callback.data.split('_')
    data = await state.get_data()
    reply = await adb.get_feedback_reply(token, callback.from_user.id)
    if data.get('feedback_reply_token') != token or not reply:
        await callback.answer('Черновик устарел. Открой отчёт заново.', show_alert=True)
        return
    if action == 'cancel':
        await adb.cancel_feedback_reply_drafts(callback.from_user.id)
        await state.clear()
        await show_feedback_detail(callback, reply['report_id'], notice='Ответ отменён.')
        return
    if action == 'edit':
        if reply['status'] in {'sending', 'sent'}:
            await callback.answer('Отправка уже началась или завершена.', show_alert=True)
            return
        fresh = await adb.create_feedback_reply(reply['report_id'], callback.from_user.id)
        if not fresh:
            await callback.answer('Открытый отчёт не найден.', show_alert=True)
            return
        await state.update_data(feedback_reply_token=fresh['token'])
        await state.set_state(Form.waiting_for_feedback_reply)
        await safe_edit_message(callback, 'Введи новый текст ответа. Предыдущий предпросмотр больше не действует.', reply_markup=kb.feedback_reply_kb(fresh['token']))
        return
    if await state.get_state() != Form.waiting_for_feedback_reply_preview.state:
        await callback.answer('Сначала подготовь предпросмотр.', show_alert=True)
        return
    claimed = await adb.claim_feedback_reply(token, callback.from_user.id)
    if not claimed:
        await callback.answer('Ответ уже обрабатывается либо отчёт закрыт или удалён.', show_alert=True)
        return
    await callback.answer('Отправляю…')
    try:
        sent = await bot.send_message(chat_id=claimed['recipient_id'],
            text=f"Ответ администратора на обращение №{claimed['report_id']}\n\n{claimed['body']}", parse_mode=None)
    except (TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter):
        await adb.finish_feedback_reply(token, 'failed')
        await safe_edit_message(callback, 'Ответ не доставлен: Telegram отклонил отправку. Можно изменить текст или отменить ответ.',
                                reply_markup=kb.feedback_reply_kb(token, True, False))
        return
    except Exception:
        await adb.finish_feedback_reply(token, 'unknown')
        logger.exception('Uncertain feedback reply delivery for report %s', claimed['report_id'])
        await safe_edit_message(callback, 'Не удалось подтвердить доставку. Ответ мог дойти. Автоматического повтора не будет. Перед новым ответом проверь доставку; повтор может создать дубликат.',
                                reply_markup=kb.feedback_reply_kb(token, True, False))
        return
    await adb.finish_feedback_reply(token, 'sent', sent.message_id)
    if (await state.get_data()).get('feedback_reply_token') == token:
        await state.clear()
        await show_feedback_detail(callback, claimed['report_id'], notice='✅ Ответ отправлен. Отчёт остаётся открытым.')


@dp.callback_query(F.data == "adm_broadcast_start")
async def cb_admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ только для администратора.", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом для отправки объявления.", show_alert=True)
        return
    await state.clear()
    await state.set_state(Form.waiting_for_broadcast)
    text = (
        "📢 <b>Новое объявление</b>\n\n"
        "Отправь текст или фотографию с подписью. Перед публикацией покажу предпросмотр и попрошу подтвердить.\n\n"
        "Обычное объявление появится в уведомлениях. Важное придёт отдельным сообщением и заменит главное меню до нажатия «Закрыть». Тип выберешь на предпросмотре. Для отмены нажми «Главное меню»."
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
        announcement_photo_file_id=message.photo[-1].file_id if message.photo else None,
        announcement_important=False,
        announcement_text=message.caption or message.text or "",
    )
    preview_text = message.caption or message.text or ""
    safe_text = html.escape(preview_text[:500]) + ("…" if len(preview_text) > 500 else "")
    preview = "📢 <b>Предпросмотр объявления</b>\n\n"
    if message.photo:
        preview += "🖼️ <i>К объявлению приложена фотография.</i>\n"
    preview += safe_text or "<i>Без текста</i>"
    preview += "\n\nТип: обычное — только раздел уведомлений.\nОпубликовать для всех подтверждённых пользователей?"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Обычное / ❗ Важное", callback_data="adm_broadcast_type")],
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="adm_broadcast_send")],
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


@dp.callback_query(F.data == "adm_broadcast_type")
async def cb_admin_broadcast_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Доступ только администратору в личном чате.", show_alert=True)
        return
    draft = await state.get_data()
    if not draft.get("announcement_message_id"):
        await callback.answer("Черновик устарел. Создай объявление заново.", show_alert=True)
        return
    important = not draft.get("announcement_important", False)
    await state.update_data(announcement_important=important)
    label = "❗ Важное — отдельное сообщение и показ вместо меню до закрытия." if important else "🔔 Обычное — только раздел уведомлений."
    preview = "📢 <b>Предпросмотр объявления</b>\n\n" + html.escape((draft.get("announcement_text") or "Без текста")[:500]) + ("…" if len(draft.get("announcement_text", "")) > 500 else "")
    if draft.get("announcement_is_photo"):
        preview += "\n🖼️ С фотографией."
    preview += "\n\n" + label + "\nОпубликовать для всех подтверждённых пользователей?"
    await show_user_screen(callback, preview, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Обычное / ❗ Важное", callback_data="adm_broadcast_type")],
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="adm_broadcast_send")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="adm_broadcast_cancel")],
    ]))


broadcast_publish_lock = None


async def send_important_announcement(bot, user_id, announcement, notify=False):
    kwargs = dict(chat_id=user_id, reply_markup=kb.important_announcement_kb(announcement["id"]),
                  parse_mode=None, disable_notification=not notify)
    if announcement.get("photo_file_id"):
        return await bot.send_photo(photo=announcement["photo_file_id"], caption=announcement["body"] or None, **kwargs)
    return await bot.send_message(text=announcement["body"], **kwargs)


async def show_important_announcement(target, user_id):
    announcement = await adb.get_unread_announcement(user_id, important=True)
    if not announcement:
        return False
    # Send the announcement itself: never reuse a menu photo as its background.
    sent = await send_important_announcement(target.bot, user_id, announcement)
    await adb.set_ui_message_id(user_id, sent.message_id)
    await cleanup_user_ui_messages(target.bot, user_id, keep_message_id=sent.message_id)
    if isinstance(target, CallbackQuery):
        await target.answer()
    return True


@dp.callback_query(F.data == "adm_broadcast_send")
async def cb_admin_broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Доступ только администратору в личном чате.", show_alert=True)
        return
    # Create the lock inside the running event loop (also supports Python 3.9).
    global broadcast_publish_lock
    if broadcast_publish_lock is None:
        broadcast_publish_lock = asyncio.Lock()
    # Claim the draft before network calls so repeated taps cannot publish twice.
    async with broadcast_publish_lock:
        draft = await state.get_data()
        if not draft.get("announcement_message_id"):
            await callback.answer("Черновик уже отправлен или отменён.", show_alert=True)
            return
        important = bool(draft.get("announcement_important"))
        announcement_id = await adb.publish_announcement(
            draft["announcement_chat_id"], draft["announcement_message_id"],
            important=important, body=draft.get("announcement_text", ""),
            photo_file_id=draft.get("announcement_photo_file_id"),
        )
        await state.clear()
    await callback.answer("Объявление опубликовано.")
    await safe_edit_message(callback, "📢 <b>Объявление опубликовано. Доставляю…</b>")
    users = await adb.get_verified_users()
    sent_count = failed_count = 0
    if important:
        announcement = dict(id=announcement_id, body=draft.get("announcement_text", ""),
                            photo_file_id=draft.get("announcement_photo_file_id"))
        for recipient in users:
            if recipient["user_id"] == callback.from_user.id:
                continue
            try:
                try:
                    sent = await send_important_announcement(bot, recipient["user_id"], announcement, notify=True)
                except TelegramRetryAfter as error:
                    await asyncio.sleep(error.retry_after)
                    sent = await send_important_announcement(bot, recipient["user_id"], announcement, notify=True)
                await adb.set_ui_message_id(recipient["user_id"], sent.message_id)
                await cleanup_user_ui_messages(bot, recipient["user_id"], keep_message_id=sent.message_id)
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed_count += 1
                logger.exception("Important announcement %s delivery failed for %s", announcement_id, recipient["user_id"])
        details = f"Доставлено: <b>{sent_count}</b>. Не удалось доставить: <b>{failed_count}</b>.\nВажный анонс будет показан в главном меню до закрытия."
    else:
        details = "Обычный анонс доступен в разделе «Уведомление». Отдельных сообщений не отправлено."
    await safe_edit_message(callback, "✅ <b>Объявление опубликовано</b>\n\n" + details,
        reply_markup=kb.admin_panel_kb(await adb.count_pending_materials(), await adb.count_pending_users(), await adb.count_open_feedback_reports()))


@dp.callback_query(F.data.startswith("important_close_"))
async def cb_important_announcement_close(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом.", show_alert=True)
        return
    try:
        announcement_id = int(callback.data[len("important_close_"):])
    except ValueError:
        await callback.answer("Некорректное объявление.", show_alert=True)
        return
    await adb.close_important_announcement(callback.from_user.id, announcement_id)
    await cb_main_menu(callback, state)


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
        markup = kb.admin_panel_kb(
            await adb.count_pending_materials(), await adb.count_pending_users(), await adb.count_open_feedback_reports()
        )
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

def main_menu_description(student_group, summary, markup):
    """Describe only the sections actually present in the user's keyboard."""
    safe_group = html.escape((student_group or '')[:40])
    group_label = f" [{safe_group}]" if safe_group else ''
    lines = [f"☝️🤓 <b>ПОМОЩНИК ПЯТНАШКИ</b> 🎓{group_label}",
             '<i>УО «БГК им. А.Е. Ларина»</i>', '']
    if summary is not None:
        lines.extend([f"📊 <b>Твои долги:</b> {summary['total'] - summary['pending']} из {summary['total']} сдано ({summary['percent']}%)", ''])
    descriptions = {
        'schedule_menu': '📅 <b>Расписание занятий</b> — общие листы замен и консультаций, история публикаций и звонки.',
        'debts_hub': '📝 <b>Трекер долгов и работ</b> — «Мои работы» для предметов и отметок о сдаче; «Методички и решения» для поиска и отправки материалов.',
        'useful_menu': '💡 <b>Полезное</b> — сообщить об ошибке, посмотреть пример заявления и скачать PDF для печати.',
        'profile_menu': '👤 <b>Профиль</b> — твой Telegram ID, выбранная группа и её изменение.',
        'notification_open': '🔔 <b>Уведомление</b> — непрочитанное объявление администратора.',
        'admin_panel': '👑 <b>Админ-панель</b> — заявки, пользователи, публикации, объявления и ответы на отчёты.',
    }
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data in descriptions:
                lines.extend([descriptions[button.callback_data], ''])
    if summary is None:
        lines.extend(['Чтобы добавить трекер для своей группы, отправь список предметов через «Полезное → Сообщить об ошибке».', ''])
    lines.append('Выбери нужный раздел:')
    return '\n'.join(lines)


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
            await state.clear()
            await state.update_data(group_action="verify")
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
    if is_verified and await show_important_announcement(message, user_id):
        return
    has_unread_notification = bool(await adb.get_unread_announcement(user_id)) if is_verified else False

    # Автоматически загружаем предметы семестра для поддерживаемых групп.
    if has_study:
        await adb.seed_default_subjects(user_id, student_group=student_group)
    
    summary = await adb.get_all_debts_summary(user_id) if has_study else None
    markup = kb.main_menu_kb(
        is_admin_user=is_adm, 
        pending_count=pending_count,
        is_verified=is_verified,
        student_group=student_group,
        is_private_chat=message.chat.type == "private",
        has_unread_notification=has_unread_notification,
    )
    
    text = main_menu_description(student_group, summary, markup)
    await show_user_screen(
        message,
        text,
        reply_markup=markup,
        photo=MAIN_MENU_PHOTO_PATH if is_verified and os.path.isfile(MAIN_MENU_PHOTO_PATH) else None,
    )

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
        "Выбери свою учебную группу из списка колледжа им. А.Е. Ларина 👇"
    )
    await state.update_data(group_action="verify")
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

    if not (await adb.get_admin_id() or ADMIN_ID):
        await state.clear()
        await show_user_screen(message, "⚠️ Не удалось отправить сообщение: администратор не настроен.", reply_markup=kb.back_to_menu_kb())
        return

    user = message.from_user
    user_label = f"@{user.username}" if user.username else (user.full_name or "Студент")
    user_record = await adb.get_user(user.id)
    body_text = message.text or message.caption or ""
    if not body_text:
        body_text = "Пользователь приложил вложение. Нажми кнопку, чтобы открыть исходное сообщение."
    try:
        await adb.save_feedback_report(
            user_id=user.id,
            user_label=user_label,
            student_group=(user_record or {}).get("student_group"),
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            body_text=body_text,
        )
    except Exception:
        logger.exception("Could not save feedback from user %s", user.id)
        await show_user_screen(message, "⚠️ Не удалось сохранить сообщение. Попробуй ещё раз чуть позже.", reply_markup=kb.back_to_menu_kb())
        return

    await state.clear()
    await show_user_screen(
        message,
        "✅ Спасибо! Отчёт сохранён и появится у администратора в отдельном разделе.",
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
    if is_verified and await show_important_announcement(callback, user_id):
        return
    has_unread_notification = bool(await adb.get_unread_announcement(user_id)) if is_verified else False
    
    markup = kb.main_menu_kb(
        is_admin_user=is_adm,
        pending_count=pending_count,
        is_verified=is_verified,
        student_group=student_group,
        is_private_chat=callback.message.chat.type == "private",
        verification_pending=verification_pending,
        has_unread_notification=has_unread_notification,
    )
    
    has_study = await user_has_study_features(callback.from_user, user_record)
    if is_verified:
        summary = await adb.get_all_debts_summary(user_id) if has_study else None
        text = main_menu_description(student_group, summary, markup)
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

    await show_user_screen(
        callback,
        text,
        reply_markup=markup,
        photo=MAIN_MENU_PHOTO_PATH if is_verified and os.path.isfile(MAIN_MENU_PHOTO_PATH) else None,
    )

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
    await state.update_data(group_action="verify")
    text = (
        "🎓 <b>Выбери свою учебную группу:</b>\n\n"
        "Выбери свою учебную группу из списка колледжа им. Ларина 👇"
    )
    await safe_edit_message(callback, text, reply_markup=kb.select_group_kb())


@dp.callback_query(F.data == "profile_menu")
async def cb_profile_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_record = await adb.get_user(callback.from_user.id)
    if not user_record or user_record.get("status") != "approved":
        await prompt_verification_required(callback, callback.from_user)
        return
    group = html.escape(user_record.get("student_group") or "не выбрана")
    username = f"@{html.escape(callback.from_user.username)}" if callback.from_user.username else "не указан"
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{html.escape(callback.from_user.full_name or 'Пользователь')}</b>\n"
        f"Telegram: {username}\n"
        f"ID: <code>{callback.from_user.id}</code>\n"
        f"🎓 Группа: <b>{group}</b>\n"
        "Статус: ✅ подтверждён"
    )
    await safe_edit_message(callback, text, reply_markup=kb.profile_menu_kb())


@dp.callback_query(F.data == "profile_change_group")
async def cb_profile_change_group(callback: CallbackQuery, state: FSMContext):
    user_record = await adb.get_user(callback.from_user.id)
    if not user_record or user_record.get("status") != "approved":
        await callback.answer("Сначала нужно пройти верификацию.", show_alert=True)
        return
    await state.clear()
    await state.update_data(group_action="change")
    current_group = html.escape(user_record.get("student_group") or "не выбрана")
    text = (
        "🎓 <b>Изменение учебной группы</b>\n\n"
        f"Текущая группа: <b>{current_group}</b>\n"
        "Выбери новую группу. Перед сохранением бот попросит подтвердить выбор."
    )
    await safe_edit_message(
        callback,
        text,
        reply_markup=kb.select_group_kb(back_callback="profile_menu", back_label="🔙 В профиль"),
    )


async def show_group_selection_confirmation(target, state: FSMContext, student_group: str, action: str):
    await state.update_data(group_action=action, selected_group=student_group)
    await state.set_state(Form.waiting_for_group_confirmation)
    safe_group = html.escape(student_group)
    if action == "change":
        user_record = await adb.get_user(target.from_user.id)
        current_group = html.escape((user_record or {}).get("student_group") or "не выбрана")
        text = (
            "🔄 <b>Подтверди смену группы</b>\n\n"
            f"Сейчас: <b>{current_group}</b>\n"
            f"Новая группа: <b>{safe_group}</b>\n\n"
            "После подтверждения доступ останется активным, а расписание будет открываться для новой группы."
        )
    else:
        text = (
            "🎓 <b>Проверь выбранную группу</b>\n\n"
            f"Ты точно хочешь выбрать группу <b>{safe_group}</b>?\n\n"
            "После подтверждения заявка отправится администратору на проверку."
        )
    if isinstance(target, CallbackQuery):
        await safe_edit_message(target, text, reply_markup=kb.confirm_group_selection_kb())
    else:
        await show_user_screen(target, text, reply_markup=kb.confirm_group_selection_kb())

@dp.callback_query(
    F.data.startswith("grp_")
    & (F.data != "grp_confirm_yes")
    & (F.data != "grp_confirm_no")
)
async def cb_select_group(callback: CallbackQuery, state: FSMContext):
    existing = await adb.get_user(callback.from_user.id)
    state_data = await state.get_data()
    action = state_data.get("group_action", "verify")
    # FSM state can be lost (for example, after /start), but a verified user
    # selecting a group is changing their profile group, not re-verifying.
    if existing and existing.get("status") == "approved" and action != "change":
        action = "change"
        await state.update_data(group_action=action)
    if existing and existing.get("status") == "pending" and action != "change":
        await callback.answer("Заявка уже на проверке. Повторно выбирать группу не нужно.", show_alert=True)
        return
    data = callback.data[len("grp_"):]
    if data == "custom":
        await state.set_state(Form.waiting_for_custom_group)
        await state.update_data(group_action=action)
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

    if action == "change" and existing and existing.get("student_group") == data:
        await state.clear()
        await callback.answer("У тебя уже выбрана эта группа. Менять ничего не нужно.")
        await safe_edit_message(
            callback,
            f"✅ В профиле уже выбрана группа <b>{html.escape(data)}</b>.",
            reply_markup=kb.profile_menu_kb(),
        )
        return
        
    await show_group_selection_confirmation(callback, state, data, action)


@dp.callback_query(F.data == "grp_confirm_no")
async def cb_group_confirmation_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("group_action", "verify")
    existing = await adb.get_user(callback.from_user.id)
    if existing and existing.get("status") == "approved" and action != "change":
        action = "change"
    await state.set_state(None)
    await state.update_data(group_action=action, selected_group=None)
    back_callback = "profile_menu" if action == "change" else "main_menu"
    back_label = "🔙 В профиль" if action == "change" else "🔙 Назад"
    text = "Выбери другую группу:" if action == "change" else "🎓 Выбери свою учебную группу:"
    await safe_edit_message(
        callback,
        text,
        reply_markup=kb.select_group_kb(back_callback=back_callback, back_label=back_label),
    )


@dp.callback_query(F.data == "grp_confirm_yes")
async def cb_group_confirmation_accept(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    student_group = (data.get("selected_group") or "").strip()
    action = data.get("group_action", "verify")
    if not student_group:
        await callback.answer("Выбери группу заново.", show_alert=True)
        await state.clear()
        await cb_start_verify_flow(callback, state)
        return

    user_id = callback.from_user.id
    existing = await adb.get_user(user_id)
    if existing and existing.get("status") == "approved" and action != "change":
        action = "change"
    if action == "change":
        if not existing or existing.get("status") != "approved":
            await state.clear()
            await callback.answer("Профиль не найден. Пройди верификацию заново.", show_alert=True)
            return
        if existing.get("student_group") == student_group:
            await state.clear()
            await callback.answer("Эта группа уже выбрана.", show_alert=True)
            return
        await adb.update_user_group(user_id, student_group)
        await adb.seed_default_subjects(user_id, student_group=student_group)
        await state.clear()
        await callback.answer("Группа изменена. Верификация сохранена.")
        text = (
            "✅ <b>Группа изменена</b>\n\n"
            f"Теперь в профиле выбрана группа <b>{html.escape(student_group)}</b>.\n"
            "Доступ и уже сохранённые данные трекера сохранены."
        )
        await safe_edit_message(callback, text, reply_markup=kb.profile_menu_kb())
        return

    if existing and existing.get("status") in {"approved", "pending"}:
        await state.clear()
        await callback.answer("Заявка уже отправлена или доступ подтверждён.", show_alert=True)
        return
    request_created = await adb.add_user_request(
        user_id=user_id,
        username=callback.from_user.username,
        tg_name=callback.from_user.full_name or "Студент",
        student_name=callback.from_user.full_name or "Студент",
        student_group=student_group,
    )
    await state.clear()
    if not request_created:
        await callback.answer("Заявка уже создана или доступ уже подтверждён.", show_alert=True)
        return
    text = (
        "✅ <b>Заявка на верификацию отправлена!</b>\n\n"
        f"🎓 Выбранная группа: <b>{html.escape(student_group)}</b>\n\n"
        "Администратор проверит заявку и сообщит о решении."
    )
    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_start")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ]))
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
        
    data = await state.get_data()
    action = data.get("group_action", "verify")
    if action == "change":
        existing = await adb.get_user(message.from_user.id)
        if existing and existing.get("status") == "approved" and existing.get("student_group") == grp:
            await state.clear()
            await show_user_screen(
                message,
                f"✅ В профиле уже выбрана группа <b>{html.escape(grp)}</b>. Менять ничего не нужно.",
                reply_markup=kb.profile_menu_kb(),
            )
            return
    await show_group_selection_confirmation(message, state, grp, action)

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
    await callback.answer("Обновляю очередь внизу чата…")
    await render_admin_verification_queue(bot, page, recreate=True)

@dp.callback_query((F.data == "adm_users_list") | F.data.startswith("adm_users_list_") | F.data.startswith("vuc_"))
async def cb_admin_users_list(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("⛔ Доступ только администратору в личном чате!", show_alert=True)
        return
    page = 0
    if callback.data.startswith("vuc_"):
        try:
            page = int(callback.data[len("vuc_"):])
        except ValueError:
            await callback.answer("Открой список групп заново.", show_alert=True)
            return
    groups = await adb.get_verified_user_groups()
    page = min(max(0, page), max(0, (len(groups) - 1) // ADMIN_PAGE_SIZE))
    if not groups:
        text = "👥 <b>Верифицированных студентов пока нет.</b>"
    else:
        text = (
            "👥 <b>Верифицированные студенты по группам</b>\n\n"
            f"Всего: <b>{sum(group['total'] for group in groups)}</b> · групп: <b>{len(groups)}</b>\n"
            f"Страница {page + 1} из {(len(groups) - 1) // ADMIN_PAGE_SIZE + 1}\n\n"
            "Выбери группу:"
        )
    await safe_edit_message(callback, text, reply_markup=kb.admin_verified_groups_kb(groups, page, ADMIN_PAGE_SIZE))


@dp.callback_query(F.data.startswith("vug_"))
async def cb_admin_verified_group(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("⛔ Доступ только администратору в личном чате!", show_alert=True)
        return
    try:
        _, token, page_value = callback.data.split("_")
        page = int(page_value)
    except (ValueError, AttributeError):
        await callback.answer("Открой список групп заново.", show_alert=True)
        return
    groups = await adb.get_verified_user_groups()
    group = next((group for group in groups if group['token'] == token), None)
    if group is None:
        await safe_edit_message(callback, "В этой группе больше нет верифицированных студентов.",
                                reply_markup=kb.admin_verified_users_page_kb(token, 0, 0))
        return
    result = await adb.get_verified_group_page(group['group_name'], page, ADMIN_PAGE_SIZE)
    page, total = result['page'], result['total']
    lines = [f"👥 <b>{html.escape(group['group_name'] or 'Без группы')}</b> — {total} чел.",
             f"Страница {page + 1} из {max(1, (total - 1) // ADMIN_PAGE_SIZE + 1)}", ""]
    for index, user in enumerate(result['users'], page * ADMIN_PAGE_SIZE + 1):
        name = html.escape(user['student_name'] or 'Студент')
        username = f" · {html.escape(user['username'])}" if user.get('username') else ""
        lines.append(f"{index}. <b>{name}</b>{username}\nID: <code>{user['user_id']}</code>")
    if not total:
        lines.append("В этой группе больше нет верифицированных студентов.")
    await safe_edit_message(callback, "\n".join(lines),
                            reply_markup=kb.admin_verified_users_page_kb(token, page, total, ADMIN_PAGE_SIZE))

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
    version_id: Optional[int] = None,
    schedule_date: Optional[datetime.date] = None,
):
    """Show the newest publication, or a requested retained version for this user."""
    if version_id is None and group_name != "ALL":
        date = schedule_date or gdrive_sync.get_target_study_date()[0]
        approved = await asyncio.to_thread(schedule_review.approved_schedule, group_name, date)
        if not approved and schedule_date is None:
            approved = await asyncio.to_thread(schedule_review.next_approved_schedule, group_name, date)
            if approved:
                date = datetime.date.fromisoformat(approved['schedule_date'])
        if approved:
            text = (f"📅 <b>Замены — {html.escape(group_name)}, {date:%d.%m.%Y}</b>\n\n"
                    f"{html.escape(approved['body'])}\n\n"
                    "✅ Текст проверен администратором.\n"
                    "Это лист замен; остальные занятия — по основному расписанию.")
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Исходные листы", callback_data="sched_view_ALL")],
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sr_view_{approved['id']}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
            ])
            await show_user_screen(target, text, reply_markup=markup)
            return
    versions = await adb.get_schedule_versions(group_name)
    if versions:
        selected = next((item for item in versions if item["id"] == version_id), None) if version_id else versions[0]
        if selected is None:
            notice = "Это старое расписание уже удалено администратором."
            selected = versions[0]
        await render_schedule_version(target, user, group_name, versions, selected,
                                      is_callback=is_callback, bot=bot, notice=notice)
        return
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
    manual_general_is_newer = manual_general_schedule_is_newer(gen_sched, target_date)
    
    markup = kb.schedule_view_kb(
        group_name=group_name, 
        is_admin_user=is_adm, 
        has_general_photo=has_gen_photo, 
        has_general_schedule=has_general_schedule,
        is_user_own_group=is_own,
        user_own_group=my_group
    )

    if manual_general_is_newer and gen_sched and gen_sched.get("schedule_text") and not gen_sched.get("photo_file_id"):
        upd_time = gen_sched.get("updated_at") or "недавно"
        text = (
            f"📢 <b>Общее расписание для всех групп {header_date}</b> 🎓\n"
            f"{note_line}"
            f"{html.escape(gen_sched['schedule_text'])}\n\n"
            f"🕒 <i>Обновлено: {html.escape(str(upd_time))}</i>"
        )
        photo_id = None
    elif gen_photo:
        upd_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"📢 <b>Общее расписание для всех групп {header_date}</b> 🎓\n"
            f"{note_line}"
            "Актуальные листы замен и консультаций — на фото ниже.\n\n"
            f"🕒 <i>Обновлено: {upd_time}</i>"
        )
        photo_id = gen_photo
    elif gen_sched and group_name != "ALL" and (gen_sched.get("schedule_text") or gen_sched.get("photo_file_id")):
        upd_time = gen_sched.get("updated_at") or "недавно"
        body = html.escape(gen_sched.get("schedule_text") or "") or "<i>(Смотри общий лист замен на фото ниже)</i>"
        safe_group_name = html.escape(group_name)
        text = (
            f"📅 <b>Расписание {header_date} — Группа {safe_group_name}</b> 🎓\n"
            f"{note_line}"
            f"📢 <b>Общее расписание для всех групп:</b>\n\n"
            f"{body}\n\n"
            f"🕒 <i>Обновлено: {upd_time}</i>"
        )
        photo_id = gen_photo
    elif sched:
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
        if not photo_id:
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

    text = f"🚧 <i>Индивидуальное расписание в разработке…</i>\n\n{text}"

    if notice:
        text = f"{notice}\n\n{text}"
        
    actual_bot = bot or getattr(target, "bot", None)
    previous_ui_id = None
    if target_chat.type == "private" and not is_callback:
        previous_ui_id = await adb.get_ui_message_id(user_id)

    async def track_schedule_screen(screen_message, related_messages=()):
        if target_chat.type != "private":
            return
        related_ids = []
        for related_message in related_messages:
            await adb.set_ui_message_id(user_id, related_message.message_id)
            related_ids.append(related_message.message_id)
        await adb.set_ui_message_id(user_id, screen_message.message_id)
        await cleanup_user_ui_messages(
            actual_bot,
            user_id,
            keep_message_id=screen_message.message_id,
            keep_message_ids=related_ids,
        )
    
    if isinstance(photo_id, list):
        if is_callback:
            try:
                await target.message.delete()
            except Exception:
                pass
        photo_messages = await actual_bot.send_media_group(
            chat_id=user_id,
            media=build_general_schedule_media(photo_id, target_date=target_date),
        )
        await cache_general_schedule_media(photo_id, photo_messages)
        screen_message = await actual_bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        await track_schedule_screen(screen_message, related_messages=photo_messages)
    elif photo_id:
        if is_callback:
            try:
                await target.message.delete()
            except Exception:
                pass
        if len(text) <= 900:
            photo_source = FSInputFile(photo_id) if os.path.isfile(photo_id) else photo_id
            if is_callback:
                screen_message = await actual_bot.send_photo(chat_id=user_id, photo=photo_source, caption=text, parse_mode="HTML", reply_markup=markup)
            else:
                screen_message = await target.answer_photo(photo=photo_source, caption=text, parse_mode="HTML", reply_markup=markup)
            if os.path.isfile(photo_id):
                await cache_general_schedule_media([photo_id], [screen_message])
        else:
            screen_message = await actual_bot.send_message(
                chat_id=target_chat.id, text=text, parse_mode="HTML", reply_markup=markup
            )
            photo_message = await actual_bot.send_photo(chat_id=target_chat.id, photo=photo_id)
            await track_schedule_screen(screen_message, related_messages=(photo_message,))
            return
        await track_schedule_screen(screen_message)
    else:
        if is_callback:
            await show_user_screen(target, text, reply_markup=markup)
        else:
            await show_user_screen(target, text, reply_markup=markup)

async def render_schedule_version(target, user, group_name, versions, selected,
                                  is_callback=False, bot=None, notice=None):
    chat = target.message.chat if is_callback else target.chat
    actual_bot = bot or target.bot
    index = versions.index(selected)
    payload = selected["payload"]
    label = "Актуальное расписание" if index == 0 else "Предыдущее расписание"
    published = datetime.datetime.fromisoformat(selected["created_at"]).replace(
        tzinfo=datetime.timezone.utc).astimezone(gdrive_sync.TIMEZONE).strftime("%d.%m.%Y %H:%M")
    text = (f"📅 <b>{label} — {html.escape(group_name)}</b>\n"
            f"Опубликовано: {published}\n\n"
            f"<b>{html.escape(payload.get('title') or 'Расписание')}</b>\n"
            f"{html.escape(payload.get('schedule_text') or '')}")
    if index:
        text += "\n\nЭто сохранённая версия. Новое расписание доступно по кнопке ниже."
    if notice:
        text = f"{notice}\n\n{text}"
    markup = kb.schedule_version_kb(
        group_name, versions[index + 1]["id"] if index + 1 < len(versions) else None,
        selected["id"], index == 0,
        is_admin_user=is_admin(user) and chat.type == "private",
    )
    photos = payload.get("photos") or []
    if not photos:
        await show_user_screen(target, text, reply_markup=markup)
        return
    related = []
    for offset in range(0, len(photos), 10):
        batch = photos[offset:offset + 10]
        def source(photo):
            value = photo["source"]
            return FSInputFile(value) if os.path.isfile(value) else value
        if len(batch) == 1:
            sent = await actual_bot.send_photo(chat_id=chat.id, photo=source(batch[0]),
                                              caption=batch[0].get("caption") or None)
            related.append(sent)
        else:
            related.extend(await actual_bot.send_media_group(chat_id=chat.id, media=[
                InputMediaPhoto(media=source(photo), caption=photo.get("caption") or None) for photo in batch
            ]))
    screen = await actual_bot.send_message(chat_id=chat.id, text=text, parse_mode="HTML", reply_markup=markup)
    if chat.type == "private":
        for sent in [*related, screen]:
            await adb.set_ui_message_id(user.id, sent.message_id)
        await cleanup_user_ui_messages(actual_bot, user.id, keep_message_id=screen.message_id,
                                       keep_message_ids=[sent.message_id for sent in related])


@dp.callback_query(F.data.startswith("schold_") | F.data.startswith("schdel_"))
async def cb_schedule_history(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, error = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, error)
        return
    try:
        action, raw_id, group = callback.data.split("_", 2)
        version_id = int(raw_id)
        if group not in {*db.COLLEGE_GROUPS, "ALL"}:
            raise ValueError
    except (ValueError, AttributeError):
        await callback.answer("Открой расписание заново.", show_alert=True)
        return
    versions = await adb.get_schedule_versions(group)
    selected = next((item for item in versions if item["id"] == version_id), None)
    if action == "schdel":
        if not is_admin(callback.from_user) or callback.message.chat.type != "private":
            await callback.answer("Удалять старое расписание может только администратор в личном чате.", show_alert=True)
            return
        deleted = selected and await adb.delete_old_schedule_version(version_id)
        await callback.answer("Старое расписание удалено." if deleted else "Расписание уже удалено или является актуальным.", show_alert=True)
        await render_group_schedule_view(callback, callback.from_user, group, is_callback=True)
        return
    await callback.answer()
    await render_group_schedule_view(callback, callback.from_user, group, is_callback=True,
                                     version_id=version_id)


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
        
    await callback.answer()
    await render_group_schedule_view(callback, callback.from_user, group_name, is_callback=True)

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
    # OCR can take longer than Telegram's callback-query deadline.
    await callback.answer()
    group_name = callback.data.replace("sched_view_", "")
    await render_group_schedule_view(callback, callback.from_user, group_name, is_callback=True)

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
async def process_group_schedule_input(message: Message, state: FSMContext, bot: Bot,
                                       schedule_album: Optional[List[Message]] = None):
    if not is_admin(message.from_user) or message.chat.type != "private":
        return
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
    
    messages = schedule_album or [message]
    photo_file_ids = [item.photo[-1].file_id for item in messages if item.photo]
    photo_file_id = photo_file_ids[0] if photo_file_ids else None
    raw_text = "\n\n".join((item.caption if item.photo else item.text).strip()
                            for item in messages if (item.caption if item.photo else item.text))
    
    if not photo_file_id and not raw_text:
        await show_user_screen(message, "⚠️ Отправь фотографию расписания или текстовое сообщение:", reply_markup=kb.back_to_menu_kb())
        return
        
    title = f"Замена / Расписание ({target_group})"
    admin_tag = f"@{message.from_user.username or ADMIN_USERNAME}"
    
    await asyncio.get_running_loop().run_in_executor(None, gdrive_sync.preserve_existing_schedule)
    changed = await adb.set_group_schedule(
        group_name=target_group,
        schedule_text=raw_text,
        title=title,
        photo_file_id=photo_file_id,
        updated_by=admin_tag,
        photo_file_ids=photo_file_ids,
    )
    await state.clear()
    if not changed:
        await render_group_schedule_view(message, message.from_user, target_group, bot=bot,
                                         notice="Это расписание уже опубликовано.")
        return
    
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
                    if len(photo_file_ids) > 1:
                        await bot.send_media_group(chat_id=user_id, media=[
                            InputMediaPhoto(media=photo, caption=broadcast_body if index == 0 else None,
                                            parse_mode="HTML")
                            for index, photo in enumerate(photo_file_ids)
                        ])
                        await bot.send_message(chat_id=user_id, text="Открыть расписание:", reply_markup=broadcast_kb)
                    elif photo_file_id:
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
        parse_mode="HTML",
        reply_markup=kb.back_to_menu_kb(),
    )
    # Treat the sync progress/report as the user's current screen too. Otherwise
    # it survives menu cleanup and remains as a permanent extra message below it.
    await adb.set_ui_message_id(callback.from_user.id, status_msg.message_id)
    await cleanup_user_ui_messages(
        bot,
        callback.from_user.id,
        keep_message_id=status_msg.message_id,
        extra_ids=(callback.message.message_id,),
    )

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, gdrive_sync.sync_drive_schedule)

    if not res.get("ok"):
        await status_msg.edit_text(
            f"⚠️ <b>Ошибка при синхронизации с Google Диском:</b>\n{res.get('error')}",
            parse_mode="HTML",
            reply_markup=kb.back_to_menu_kb(),
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
    await status_msg.edit_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Черновики замен", callback_data="sr_queue")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ]))
    await notify_synced_schedule(bot, res)
    await prepare_and_notify_schedule_reviews(bot)


def schedule_review_kb(review_id, photo_count=0):
    rows = [
        [InlineKeyboardButton(text="✏️ Исправить текст", callback_data=f"sr_edit_{review_id}")],
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"sr_publish_{review_id}"),
         InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"sr_skip_{review_id}")],
    ]
    for index in range(photo_count):
        rows.append([InlineKeyboardButton(text=f"📸 Исходный лист {index + 1}", callback_data=f"sr_photo_{review_id}_{index}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


review_screen_lock = None


async def update_review_screen(bot, chat_id, text, markup):
    global review_screen_lock
    if review_screen_lock is None:
        review_screen_lock = asyncio.Lock()
    async with review_screen_lock:
        await _update_review_screen(bot, chat_id, text, markup)


async def _update_review_screen(bot, chat_id, text, markup):
    message_id = await asyncio.to_thread(schedule_review.screen_id, chat_id)
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                        parse_mode="HTML", reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                return
            logger.info("Review screen unavailable: %s", error)
    sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    await asyncio.to_thread(schedule_review.screen_id, chat_id, 'queue', sent.message_id)


async def send_schedule_review(bot, chat_id, draft, remaining=None, notice=''):
    import json
    # Keep the optional source photo aligned with the current draft, without
    # sending another photo for every item or deleting any old chat messages.
    photo_id = await asyncio.to_thread(schedule_review.screen_id, chat_id, 'photo')
    sources = json.loads(draft['sources'])
    if photo_id:
        try:
            if sources:
                await bot.edit_message_media(chat_id=chat_id, message_id=photo_id,
                    media=InputMediaPhoto(media=FSInputFile(sources[0]['source']),
                        caption=f"{draft['group_name']} — {draft['schedule_date']}. " + sources[0].get('caption', ''), parse_mode=None))
            else:
                await bot.edit_message_caption(chat_id=chat_id, message_id=photo_id,
                    caption="У текущего черновика нет исходного фото. Показан лист предыдущего черновика.", parse_mode=None)
        except TelegramBadRequest as error:
            logger.info("Review source photo unavailable: %s", error)
    body = html.escape(draft['body']) if draft['body'] else "⚠️ Текст не прочитан. Нажми «Исправить текст»."
    text = (f"{html.escape(notice)}\n" if notice else '') + (
        f"📝 <b>Черновик замен — {html.escape(draft['group_name'])}, {draft['schedule_date']}</b>\n"
        f"Осталось на проверке: {remaining if remaining is not None else 1}\n\n{body}\n\n"
        "Сверь текст с исходными листами. Студенты увидят его только после публикации.")
    await update_review_screen(bot, chat_id, text, schedule_review_kb(draft['id'], len(json.loads(draft['sources']))))


async def show_next_schedule_review(bot, chat_id, notice=''):
    drafts = await asyncio.to_thread(schedule_review.pending_reviews)
    if drafts:
        await send_schedule_review(bot, chat_id, drafts[0], len(drafts), notice)
    else:
        await update_review_screen(bot, chat_id, (html.escape(notice) + "\n\n" if notice else '') +
            "✅ Все черновики обработаны.", kb.back_to_menu_kb())


async def prepare_and_notify_schedule_reviews(bot):
    try:
        await asyncio.to_thread(schedule_review.prepare_reviews)
    except schedule_review.GeminiError as error:
        logger.warning("Draft preparation failed: %s", error)
        if ADMIN_ID:
            await update_review_screen(bot, ADMIN_ID, "⚠️ Черновики Gemini не подготовлены: " + html.escape(str(error)),
                                       kb.back_to_menu_kb())
        return
    if ADMIN_ID:
        drafts = await asyncio.to_thread(schedule_review.pending_reviews, True)
        if drafts:
            await show_next_schedule_review(bot, ADMIN_ID)
            for draft in drafts:
                await asyncio.to_thread(schedule_review.mark_notified, draft['id'])


delivery_lock = None


async def deliver_schedule_notifications(bot):
    global delivery_lock
    if delivery_lock is None:
        delivery_lock = asyncio.Lock()
    sent = failed = 0
    async with delivery_lock:
        for item in await asyncio.to_thread(schedule_review.pending_deliveries):
            try:
                kwargs = dict(chat_id=item['user_id'], text=item['body'], parse_mode=None,
                    disable_notification=False, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Открыть расписание", callback_data=item['callback_data'])]]))
                try:
                    await bot.send_message(**kwargs)
                except TelegramRetryAfter as error:
                    await asyncio.sleep(error.retry_after)
                    await bot.send_message(**kwargs)
                await asyncio.to_thread(schedule_review.delivery_sent, item['event_key'], item['user_id'])
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
                logger.exception("Schedule notification failed for event %s user %s", item['event_key'], item['user_id'])
    return sent, failed


async def notify_synced_schedule(bot, result):
    if result.get('new_downloaded', 0) > 0:
        import hashlib, json
        fingerprint = hashlib.sha256(json.dumps(sorted([tuple(str(item.get(key, '')) for key in ('file_id', 'filename', 'doc_type', 'schedule_date', 'part_number', 'drive_timestamp')) for item in result.get('files', [])]), default=str).encode()).hexdigest()
        await asyncio.to_thread(schedule_review.enqueue_delivery, f"drive:{fingerprint}",
            "📅 В боте появились новые листы расписания. Открой исходные листы; текст замен по группам появится после проверки администратора.",
            "sched_view_ALL")
    return await deliver_schedule_notifications(bot)


@dp.callback_query(F.data == "sr_queue")
async def cb_schedule_review_queue(callback: CallbackQuery, state: FSMContext = None):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Только для администратора в личном чате.", show_alert=True)
        return
    await callback.answer()
    if state:
        await state.clear()
    await show_next_schedule_review(callback.bot, callback.from_user.id)


@dp.callback_query(F.data.startswith("sr_photo_"))
async def cb_schedule_review_photo(callback: CallbackQuery):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Только для администратора в личном чате.", show_alert=True)
        return
    import json
    try:
        _, _, raw_id, raw_index = callback.data.split('_')
        draft = await asyncio.to_thread(schedule_review.get_review, int(raw_id))
        index = int(raw_index)
        if not draft or draft['status'] != 'draft' or index < 0:
            raise ValueError
        photo = json.loads(draft['sources'])[index]
    except (ValueError, IndexError, TypeError):
        await callback.answer("Открой актуальную очередь черновиков.", show_alert=True)
        return
    await callback.answer()
    saved = await asyncio.to_thread(schedule_review.screen_id, callback.from_user.id, 'photo')
    media = InputMediaPhoto(media=FSInputFile(photo['source']), caption=photo.get('caption'), parse_mode=None)
    if saved:
        try:
            await callback.bot.edit_message_media(chat_id=callback.from_user.id, message_id=saved, media=media)
            return
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                return
    sent = await callback.bot.send_photo(chat_id=callback.from_user.id, photo=FSInputFile(photo['source']),
                                         caption=photo.get('caption'), parse_mode=None)
    await asyncio.to_thread(schedule_review.screen_id, callback.from_user.id, 'photo', sent.message_id)


@dp.callback_query(F.data.startswith("sr_edit_") | F.data.startswith("sr_publish_") | F.data.startswith("sr_skip_"))
async def cb_schedule_review_action(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user) or callback.message.chat.type != "private":
        await callback.answer("Только для администратора в личном чате.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split('_', 2)
        review_id = int(raw_id)
    except ValueError:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    draft = await asyncio.to_thread(schedule_review.get_review, review_id)
    if not draft or draft['status'] != 'draft' or draft['schedule_date'] < gdrive_sync.get_target_study_date()[0].isoformat():
        await callback.answer("Черновик уже обработан, устарел или заменён новым листом.", show_alert=True)
        return
    if action == 'publish' and not draft['body'].strip():
        await callback.answer("Сначала введи текст через «Исправить текст».", show_alert=True)
        return
    await callback.answer()
    if action == 'edit':
        await state.set_state(Form.waiting_for_schedule_review)
        await state.update_data(schedule_review_id=review_id)
        await update_review_screen(callback.bot, callback.from_user.id,
            f"Отправь полный исправленный текст замен для {html.escape(draft['group_name'])} на {draft['schedule_date']}. "
            f"До {schedule_review.MAX_BODY} символов. Для отмены — /cancel.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить правку", callback_data="sr_queue")]]))
        return
    changed = await asyncio.to_thread(schedule_review.finish_review, review_id, action == 'publish')
    if not changed:
        return
    await state.clear()
    notice = "Черновик пропущен. Его текст не опубликован."
    if action == 'publish':
        sent, failed = await deliver_schedule_notifications(callback.bot)
        notice = f"Текст опубликован. Уведомлений доставлено: {sent}, не доставлено: {failed}."
    await show_next_schedule_review(callback.bot, callback.from_user.id, notice)


@dp.message(Form.waiting_for_schedule_review)
async def process_schedule_review_edit(message: Message, state: FSMContext):
    if not is_admin(message.from_user) or message.chat.type != "private":
        return
    if message.text and message.text.startswith('/'):
        await state.clear()
        await show_next_schedule_review(message.bot, message.chat.id, "Редактирование отменено.")
        return
    data = await state.get_data()
    review_id = data.get('schedule_review_id')
    if not message.text or not message.text.strip() or len(message.text.strip()) > schedule_review.MAX_BODY:
        await update_review_screen(message.bot, message.chat.id, f"Отправь текст от 1 до {schedule_review.MAX_BODY} символов.", kb.back_to_menu_kb())
        return
    saved = await asyncio.to_thread(schedule_review.edit_review, review_id, message.text)
    await state.clear()
    if not saved:
        await show_next_schedule_review(message.bot, message.chat.id, "Черновик уже обработан или заменён новым листом.")
        return
    draft = await asyncio.to_thread(schedule_review.get_review, review_id)
    await send_schedule_review(message.bot, message.chat.id, draft)


@dp.callback_query(F.data.startswith("sr_view_"))
async def cb_published_schedule_review(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    access_ok, error = await check_user_access(callback.from_user)
    if not access_ok:
        await prompt_verification_required(callback, callback.from_user, error)
        return
    await callback.answer()
    try:
        review_id = int(callback.data[len('sr_view_'):])
    except ValueError:
        return
    draft = await asyncio.to_thread(schedule_review.get_review, review_id)
    if not draft or draft['status'] != 'published':
        await show_user_screen(callback, "Эта публикация уже заменена новым листом. Открой актуальное расписание.", reply_markup=kb.back_to_menu_kb())
        return
    await render_group_schedule_view(callback, callback.from_user, draft['group_name'], is_callback=True,
        schedule_date=datetime.date.fromisoformat(draft['schedule_date']))


# Совместимость со старыми кнопками
@dp.callback_query(F.data.in_(["sched_today", "sched_tomorrow", "sched_week"]) | F.data.startswith("sched_day_"))
async def cb_legacy_schedule(callback: CallbackQuery, state: FSMContext):
    await cb_schedule_menu(callback, state)

# =========================================================================
# --- ТРЕКЕР ДОЛГОВ И УЧЕБНЫХ РАБОТ (ХАБ) ---
# =========================================================================

@dp.callback_query(F.data == "useful_menu")
async def cb_useful_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "💡 <b>Полезное</b>\n\nВыбери нужный пункт:"
    await show_user_screen(callback, text, reply_markup=kb.useful_hub_kb())


@dp.callback_query(F.data == "useful_application")
async def cb_useful_application(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    user_id = callback.from_user.id
    if not os.path.isfile(APPLICATION_EXAMPLE_PHOTO) or not os.path.isfile(APPLICATION_TEMPLATE_PDF):
        await show_user_screen(
            callback,
            "⚠️ Образец заявления временно недоступен.",
            reply_markup=kb.useful_hub_kb(),
        )
        return

    try:
        photo_message = await callback.bot.send_photo(
            chat_id=user_id,
            photo=FSInputFile(APPLICATION_EXAMPLE_PHOTO),
            caption="📷 Фото исходного заявления для примера.",
        )
        file_message = await callback.bot.send_document(
            chat_id=user_id,
            document=FSInputFile(APPLICATION_TEMPLATE_PDF),
            caption="📄 Чистый шаблон заявления для печати.",
            reply_markup=kb.useful_application_kb(),
        )
    except Exception:
        logger.exception("Could not send the application example to user %s", user_id)
        await show_user_screen(
            callback,
            "⚠️ Не удалось отправить образец. Попробуй ещё раз.",
            reply_markup=kb.useful_hub_kb(),
        )
        return

    await adb.set_ui_message_id(user_id, photo_message.message_id)
    await adb.set_ui_message_id(user_id, file_message.message_id)
    await cleanup_user_ui_messages(
        callback.bot,
        user_id,
        keep_message_ids=(photo_message.message_id, file_message.message_id),
        extra_ids=(callback.message.message_id,),
    )


@dp.callback_query(F.data == "notification_open")
async def cb_notification_open(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer("Открой уведомление в личном чате с ботом.", show_alert=True)
        return
    announcement = await adb.get_unread_announcement(callback.from_user.id)
    if not announcement:
        await callback.answer("Новых уведомлений пока нет.", show_alert=True)
        return

    try:
        copied = await callback.bot.copy_message(
            chat_id=callback.from_user.id,
            from_chat_id=announcement["source_chat_id"],
            message_id=announcement["source_message_id"],
            reply_markup=kb.notification_message_kb(),
        )
    except Exception:
        logger.exception("Could not open announcement %s for user %s", announcement["id"], callback.from_user.id)
        await callback.answer("Не удалось открыть уведомление. Сообщи администратору.", show_alert=True)
        return

    await adb.mark_announcement_read(callback.from_user.id, announcement["id"])
    await adb.set_ui_message_id(callback.from_user.id, copied.message_id)
    await cleanup_user_ui_messages(
        callback.bot,
        callback.from_user.id,
        keep_message_id=copied.message_id,
        extra_ids=(callback.message.message_id,),
    )
    await callback.answer()

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
    summary = await adb.get_all_debts_summary(user_id)
    text = (
        "📝 <b>Моя учёба</b>\n\n"
        f"Осталось сдать: {summary['pending']} · Сдано: {summary['total'] - summary['pending']} из {summary['total']}\n\n"
        f"{study_empty_status(summary['total'], summary['pending'])}\n\n"
        "Мои работы — предметы, задания и отметки о сдаче\n"
        "Методички и решения — найти материал или отправить свой"
    )
    await safe_edit_message(callback, text, reply_markup=kb.debts_hub_kb())


def study_empty_status(total: int, pending: int) -> str:
    if total == 0:
        return "Работ пока нет. Открой предмет, чтобы добавить первую"
    if pending == 0:
        return "Все добавленные работы сданы"
    return "Открой «Мои работы», чтобы отметить сдачу"


# --- Показать все долги ---
@dp.callback_query(F.data == "show_all_debts")
@dp.callback_query(F.data.regexp(r"^show_all_debts_\d+$"))
async def cb_show_all_debts(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    summary = await adb.get_all_debts_summary(user_id)
    
    page_size = 10
    requested_page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "show_all_debts" else 0
    page = min(requested_page, max(0, (summary["pending"] - 1) // page_size))
    markup = kb.debt_tasks_page_kb(page, summary["pending"], page_size)

    if summary["total"] == 0:
        text = "📊 <b>Все долги</b>\n\n" + study_empty_status(0, 0)
    elif summary["pending"] == 0:
        text = "📊 <b>Все долги</b>\n\n" + study_empty_status(summary['total'], 0)
    else:
        text = (
            f"📋 <b>Все долги</b>\n\n"
            f"🔴 <b>Осталось сдать:</b> {summary['pending']} из {summary['total']} работ\n"
            f"📈 <b>Прогресс сдачи:</b> <code>[{summary['bar']}]</code> {summary['percent']}%\n\n"
            f"─── <b>Несданные работы:</b> ───\n"
        )
        tasks = await adb.get_pending_tasks_page(user_id, page_size, page * page_size)
        for task in tasks:
            safe_sub = html.escape(task["subject_name"][:60])
            safe_task = html.escape(task["title"][:160])
            text += f"\n📚 <b>{safe_sub}</b> — ❌ {safe_task}\n"
        text += f"\nСтраница {page + 1} из {max(1, (summary['pending'] + page_size - 1) // page_size)}\n"

        text += "\n💡 <i>Чтобы отметить работу или посмотреть методички, перейди в «Мои работы».</i>"

    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=markup)


# --- Список предметов ---
@dp.callback_query(F.data == "list_subjects")
async def cb_list_subjects(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_tt36_student(callback.from_user):
        await reject_non_tt36(callback)
        return

    user_id = callback.from_user.id
    subjects = await adb.get_subjects_with_counts(user_id)
    
    summary = await adb.get_all_debts_summary(user_id)
    text = (
        "📚 <b>Мои работы</b>\n\n"
        "Выбери предмет: рядом указано количество несданных работ.\n"
        "🔴 — есть несданные работы · ✅ — всё сдано · ⚪ — работ пока нет\n\n"
        + study_empty_status(summary['total'], summary['pending'])
    )
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.subjects_list_kb(subjects))


# --- Открыть предмет и его работы ---
@dp.callback_query(F.data.startswith("open_sub_"))
async def cb_open_subject(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
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
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (не сдано: <b>{pending_count}</b>)\n\n"
        f"• Нажатие <b>слева</b> переключает статус сдачи (❌ / ✅)\n"
        f"• Кнопка <b>справа 📚</b> открывает проверенные методички и решения к работе!"
    )
    
    if total_tasks == 0 or pending_count == 0:
        text += '\n\n' + study_empty_status(total_tasks, pending_count)
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.subject_tasks_kb(subject_id, tasks, mat_counts, page, total_tasks, page_size))


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
    
    status_msg = "✅ Работа сдана" if new_status else "❌ Работа отмечена как несданная"
    
    text = (
        f"📖 <b>Предмет:</b> {safe_name}\n"
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (не сдано: <b>{pending_count}</b>)\n\n"
        f"• Нажатие <b>слева</b> переключает статус сдачи (❌ / ✅)\n"
        f"• Кнопка <b>справа 📚</b> открывает проверенные методички и решения к работе!"
    )
    
    if total_tasks == 0 or pending_count == 0:
        text += '\n\n' + study_empty_status(total_tasks, pending_count)
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.subject_tasks_kb(subject_id, tasks, mat_counts, page, total_tasks, page_size))
    await callback.answer(status_msg)

# =========================================================================
# --- БЛОК: МЕТОДИЧКИ И РЕШЕНИЯ (МАТЕРИАЛЫ) ---
# =========================================================================

@dp.callback_query(F.data.startswith("view_mat_"))
async def cb_view_materials(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
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
            f"Будь первым! Нажми <b>«📤 Отправить свой материал»</b>, загрузи фото или файл, "
            f"и после проверки администратором он станет доступен всем студентам!"
        )
    else:
        text = (
            f"📚 <b>Проверенные материалы к работе:</b>\n"
            f"«{safe_title}» (<i>{safe_sub}</i>)\n\n"
            f"Нажми на кнопку материала ниже, чтобы скачать методичку или готовое решение 👇"
        )
        
    await safe_edit_message(callback,
        text, parse_mode="HTML",
        reply_markup=kb.materials_list_kb(task_id, subject_id, materials, page, material_total, 25)
    )


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
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=kb.admin_panel_kb(
                await adb.count_pending_materials(), await adb.count_pending_users(), await adb.count_open_feedback_reports()
            ),
        )
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
    total_approved = sum(item["count"] for item in subjects_with_counts)
    
    text = (
        f"📖 <b>Методички и решения{html.escape(grp_badge)}</b> 🎓\n\n"
        f"Здесь собраны все проверенные материалы по учёбе нашей группы (всего доступно: <b>{total_approved}</b> шт.).\n\n"
        "🟢 — есть проверенные материалы\n"
        "⚪ — пока пусто (можно отправить свой материал)\n\n"
        "Выбери предмет, чтобы открыть список файлов 👇"
    )
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.materials_hub_kb(subjects_with_counts))


@dp.callback_query(F.data.startswith("b_sub_"))
async def cb_browse_subject_materials(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
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
        
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.subject_materials_view_kb(subject_name, materials))


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
        callback = callback.model_copy(update={"data": f"quick_sub_{sub['id']}"})
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
        f"📤 <b>Отправить свой материал{html.escape(grp_badge)}</b>\n\n"
        "Поделись материалами к учёбе с одногруппниками! После верификации администратором "
        "материал появится в общей базе бота и поможет всем подготовиться к работам.\n\n"
        "Выбери предмет из списка ниже 👇"
    )
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.quick_subjects_select_kb(subjects))


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
        await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.study_back_kb("quick_upload_material", "🔙 К выбору предмета"))
    else:
        text = (
            f"📚 <b>Предмет:</b> {safe_name}\n\n"
            f"Выбери работу, к которой хочешь прикрепить методичку или решение 👇"
        )
        await safe_edit_message(callback,
            text, parse_mode="HTML",
            reply_markup=kb.quick_tasks_select_kb(subject_id, tasks, page, total_tasks, page_size)
        )
        


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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.choose_material_type_kb(task_id, subject_id))


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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.study_back_kb("quick_upload_material", "🔙 К выбору предмета"))


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
        await show_user_screen(message, "⚠️ Отправь название текстом или прикрепи фотографию/файл с подписью.", reply_markup=kb.study_back_kb("quick_upload_material", "🔙 К выбору предмета"))
        return
    subject = await adb.get_subject_by_id(user_id, subject_id) if subject_id else None
    if not subject:
        await state.clear()
        await show_user_screen(message, "⚠️ Предмет не найден или уже удалён. Начни загрузку заново.", reply_markup=kb.study_back_kb("quick_upload_material", "🔙 К выбору предмета"))
        return

    task_id = await adb.add_task(user_id, subject_id, raw_title)
    if task_id is None:
        await state.clear()
        await show_user_screen(message, "⚠️ Не удалось создать работу. Проверь предмет и попробуй ещё раз.", reply_markup=kb.study_back_kb("quick_upload_material", "🔙 К выбору предмета"))
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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.choose_material_type_kb(task_id, subject_id))


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
        await safe_edit_message(callback,
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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.study_back_kb(f"view_mat_{task_id}_{subject_id}", "🔙 К материалам работы"))


@dp.message(Form.waiting_for_material_title)
async def process_material_title(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    data = await state.get_data()
    task_id, subject_id = data.get("task_id"), data.get("subject_id")
    if message.photo or message.document:
        await process_material_file(message, state, bot)
        return
    if not message.text or not message.text.strip():
        await show_user_screen(message, "⚠️ Отправь название текстом или прикрепи фотографию/файл.", reply_markup=kb.study_back_kb(f"view_mat_{task_id}_{subject_id}", "🔙 К материалам работы"))
        return

    title = message.text.strip()
    await state.update_data(mat_title=title)
    await state.set_state(Form.waiting_for_material_file)
    
    text = (
        f"✍️ Название <b>«{html.escape(title)}»</b> принято!\n\n"
        f"📸 Теперь <b>скинь фотографию</b> (или файл) 📷 👇\n\n"
        f"<i>(или отправь /cancel для отмены)</i>"
    )
    await show_user_screen(message, text, reply_markup=kb.study_back_kb(f"view_mat_{task_id}_{subject_id}", "🔙 К материалам работы"))

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
        await show_user_screen(message, "⚠️ Работа больше недоступна. Начни загрузку материала заново.", reply_markup=kb.study_back_kb("browse_materials_hub", "🔙 К методичкам и решениям"))
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
        await show_user_screen(message, "⚠️ Пожалуйста, просто скинь фотографию, документ или текст.", reply_markup=kb.study_back_kb(f"view_mat_{task_id}_{subject_id}", "🔙 К материалам работы"))
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
        await show_user_screen(message, "⚠️ Не удалось сохранить материал: работа не найдена.", reply_markup=kb.study_back_kb(f"view_mat_{task_id}_{subject_id}", "🔙 К материалам работы"))
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
    await safe_edit_message(callback,
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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.study_back_kb())


@dp.message(Form.waiting_for_subject_name)
async def process_subject_name(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await cmd_start(message, state)
        return
        
    user_id = message.from_user.id
    name = (message.text or message.caption or "").strip()
    if not name:
        await show_user_screen(message, "⚠️ Название предмета не может быть пустым. Попробуй ещё раз:", reply_markup=kb.study_back_kb())
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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.back_to_subject_kb(subject_id))


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
        await show_user_screen(message, "⚠️ Предмет не найден или уже удалён. Начни добавление работы заново.", reply_markup=kb.study_back_kb())
        return
    
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text:
        await show_user_screen(message, "⚠️ Отправь название работы текстом. Можно указать несколько строк.", reply_markup=kb.study_back_kb(f"open_sub_{subject_id}", "🔙 К работам предмета"))
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
        f"📊 <b>Статус:</b> {total_tasks - pending_count}/{total_tasks} сдано (не сдано: <b>{pending_count}</b>)\n\n"
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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.confirm_delete_subject_kb(subject_id))


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
    await safe_edit_message(callback, text, parse_mode="HTML", reply_markup=kb.subjects_list_kb(subjects))


async def gdrive_sync_background_loop(bot: Bot):
    """Фоновый опрос Google Диска: проверяет новые файлы каждые 30 минут."""
    await asyncio.sleep(15)
    while True:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, gdrive_sync.sync_drive_schedule)
            if res.get("ok"):
                await notify_synced_schedule(bot, res)
                await prepare_and_notify_schedule_reviews(bot)
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

