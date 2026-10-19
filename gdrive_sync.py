import os
import re
import json
import ssl
import logging
import datetime
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
import urllib.request
import certifi

import database as db

logger = logging.getLogger(__name__)

DEFAULT_FOLDER_ID = "1Xh7j66Z7XGCLrrKxfqjlc5VJ8RcnLyQe"
FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", DEFAULT_FOLDER_ID)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "assets", "schedule", "downloads")

TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Minsk"))
CUTOFF_HOUR = int(os.getenv("SCHEDULE_CUTOFF_HOUR", "17"))
SATURDAY_CUTOFF_HOUR = int(os.getenv("SCHEDULE_SATURDAY_CUTOFF_HOUR", "14"))

WEEKDAY_NAMES_RU = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье"
}


def parse_filename_meta(filename: str) -> Dict:
    """Извлекает тип документа, дату расписания и номер части из имени файла"""
    base, ext = os.path.splitext(filename)
    lower = base.lower()

    doc_type = "consultations" if "консультаци" in lower else "replacement"

    # Извлечение дня и месяца (например: 19-10, 19.10)
    m_date = re.search(r'(\d{1,2})[-.](\d{1,2})(?:[-.](\d{2,4}))?', base)
    schedule_date_str = None
    schedule_date = None
    part_number = 1

    current_year = datetime.datetime.now().year

    if m_date:
        day = int(m_date.group(1))
        month = int(m_date.group(2))
        year = int(m_date.group(3)) if m_date.group(3) else current_year
        if year < 100:
            year += 2000
        try:
            schedule_date = datetime.date(year, month, day)
            schedule_date_str = schedule_date.isoformat()
        except ValueError:
            pass

        remainder = base[m_date.end():]
        m_part = re.search(r'[-_](\d+)', remainder)
        if m_part:
            part_number = int(m_part.group(1))

    return {
        "doc_type": doc_type,
        "schedule_date": schedule_date_str,
        "schedule_date_obj": schedule_date,
        "part_number": part_number,
    }


def fetch_drive_files_list(folder_id: Optional[str] = None) -> List[Dict]:
    """Получает список доступных файлов из открытой папки Google Диска без API-ключей."""
    target_folder_id = folder_id or FOLDER_ID
    url = f"https://drive.google.com/drive/folders/{target_folder_id}"

    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        logger.error(f"Error fetching Google Drive folder HTML: {e}")
        return []

    prefix = b"window['_DRIVE_ivd'] = '"
    start = raw_bytes.find(prefix)
    if start == -1:
        logger.warning("Could not find _DRIVE_ivd in Google Drive folder HTML")
        return []

    start += len(prefix)
    end = raw_bytes.find(b"';", start)
    if end == -1:
        return []

    val = raw_bytes[start:end]
    # Заменяем \xHH на соответствующие байты
    try:
        unescaped = re.sub(rb'\\x([0-9a-fA-F]{2})', lambda m: bytes([int(m.group(1), 16)]), val)
        text = unescaped.decode('utf-8', errors='ignore')
        data = json.loads(text)
    except Exception as e:
        logger.error(f"Error decoding _DRIVE_ivd JSON: {e}")
        return []

    files_raw = data[0] if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list) else []
    result = []

    for item in files_raw:
        if isinstance(item, list) and len(item) >= 4:
            file_id = item[0]
            filename = item[2]
            mime_type = item[3]
            drive_ts = item[9] if len(item) > 9 and isinstance(item[9], (int, float)) else 0
            file_size = item[12] if len(item) > 12 and isinstance(item[12], int) else 0

            # Фильтруем только файлы расписания (изображения или pdf)
            ext = os.path.splitext(filename)[1].lower()
            if ext in [".jpeg", ".jpg", ".png", ".pdf"]:
                meta = parse_filename_meta(filename)
                result.append({
                    "file_id": file_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "drive_timestamp": int(drive_ts),
                    "file_size": file_size,
                    **meta
                })

    logger.info(f"Found {len(result)} schedule files in Google Drive folder {target_folder_id}")
    return result


def download_file(file_id: str, dest_path: str) -> bool:
    """Скачивает файл с Google Диска по прямой ссылке"""
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    temp_path = f"{dest_path}.tmp"

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp, open(temp_path, "wb") as out:
            out.write(resp.read())
        os.replace(temp_path, dest_path)
        logger.info(f"Successfully downloaded {dest_path} ({os.path.getsize(dest_path)} bytes)")
        return True
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


def sync_drive_schedule(force: bool = False) -> Dict:
    """Синхронизирует расписание с Google Диском: находит и скачивает новые файлы."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    files = fetch_drive_files_list()
    
    if not files:
        return {
            "ok": False,
            "error": "Не удалось получить список файлов с Google Диска или папка пуста",
            "total_found": 0,
            "new_downloaded": 0,
            "files": []
        }

    new_downloaded = 0
    updated_files = []

    for f in files:
        file_id = f["file_id"]
        filename = f["filename"]
        # Безопасное имя файла
        safe_name = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ_.\- ]', '_', filename)
        dest_path = os.path.join(DOWNLOADS_DIR, f"{file_id}_{safe_name}")

        existing = db.get_gdrive_file(file_id)
        needs_download = force or not existing or not os.path.exists(existing.get("local_path", ""))

        if needs_download:
            success = download_file(file_id, dest_path)
            if success:
                f_size = os.path.getsize(dest_path)
                db.save_gdrive_file(
                    file_id=file_id,
                    filename=filename,
                    doc_type=f["doc_type"],
                    schedule_date=f["schedule_date"],
                    part_number=f["part_number"],
                    local_path=dest_path,
                    file_size=f_size,
                    drive_timestamp=f["drive_timestamp"]
                )
                new_downloaded += 1
                updated_files.append(f)
        else:
            # Обновляем метаданные если уже есть
            db.save_gdrive_file(
                file_id=file_id,
                filename=filename,
                doc_type=f["doc_type"],
                schedule_date=f["schedule_date"],
                part_number=f["part_number"],
                local_path=existing["local_path"],
                file_size=existing.get("file_size", 0),
                drive_timestamp=f["drive_timestamp"]
            )

    return {
        "ok": True,
        "total_found": len(files),
        "new_downloaded": new_downloaded,
        "files": files,
        "updated_files": updated_files
    }


def get_target_study_date(now: Optional[datetime.datetime] = None) -> Tuple[datetime.date, bool, str]:
    """
    Определяет целевой учебный день с учётом правила отсечки прошедшего дня (после 17:00, в субботу после 14:00):
    - В субботу после 14:00 (или 17:00) суббота завершена -> переключаемся на понедельник.
    - В воскресенье занятий нет -> переключаемся на понедельник.
    - В будние дни (Пн-Чт) после 17:00 учебный день завершён -> переключаемся на завтра.
    - В пятницу после 17:00 -> переключаемся на субботу.
    - В учебные часы возвращается текущая дата.
    
    Возвращает: (target_date, is_next_day, reason_text)
    """
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)

    weekday = now.weekday()  # 0=Пн ... 5=Сб, 6=Вс

    is_sat_over = (weekday == 5 and (now.hour >= SATURDAY_CUTOFF_HOUR or now.hour >= CUTOFF_HOUR))
    is_sunday = (weekday == 6)
    is_weekday_over = (weekday < 5 and now.hour >= CUTOFF_HOUR)

    if is_sat_over:
        target = now.date() + datetime.timedelta(days=2)
        reason = "Учебный день субботы завершён — показываем расписание на понедельник"
        return target, True, reason
    elif is_sunday:
        target = now.date() + datetime.timedelta(days=1)
        reason = "Сегодня выходной (воскресенье) — показываем расписание на понедельник"
        return target, True, reason
    elif is_weekday_over:
        if weekday == 4:  # Пятница -> суббота
            target = now.date() + datetime.timedelta(days=1)
            reason = f"Учебный день пятницы завершён (после {CUTOFF_HOUR}:00) — показываем расписание на субботу"
        else:
            target = now.date() + datetime.timedelta(days=1)
            reason = f"Учебный день завершён (после {CUTOFF_HOUR}:00) — показываем расписание на завтра"
        return target, True, reason
    else:
        day_name = WEEKDAY_NAMES_RU.get(weekday, "сегодня")
        reason = f"Расписание на сегодня ({day_name})"
        return now.date(), False, reason


def get_active_gdrive_schedule_photos(target_date: Optional[datetime.date] = None) -> List[Tuple[str, str, datetime.date]]:
    """Возвращает список актуальных фото замен из Google Диска на целевую дату (прошедшие даты отсекаются)."""
    if target_date is None:
        target_date, _, _ = get_target_study_date()

    target_date_str = target_date.isoformat()
    # Запрашиваем файлы строго для target_date или ближайшей предстоящей даты (не в прошлом!)
    db_files = db.get_latest_gdrive_schedule_files(doc_type="replacement", min_date=target_date_str)
    if not db_files:
        return []

    result = []
    for f in db_files:
        path = f.get("local_path")
        if not path or not os.path.isfile(path):
            continue

        date_str = f.get("schedule_date")
        valid_until = target_date
        if date_str:
            try:
                valid_until = datetime.date.fromisoformat(date_str)
            except ValueError:
                pass

        part = f.get("part_number", 1)
        formatted_date = valid_until.strftime("%d.%m.%Y")
        caption = f"Замены на {formatted_date}"
        if part > 1:
            caption += f" — часть {part}"

        result.append((path, caption, valid_until))

    return result

