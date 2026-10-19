import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(__file__), "debts.db")
DEFAULT_SCHEDULE_MAX_AGE_DAYS = 14

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("lower", 1, lambda s: s.lower() if s is not None else None)
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                is_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                task_id INTEGER NOT NULL,
                material_type TEXT NOT NULL,
                title TEXT NOT NULL,
                file_id TEXT,
                file_type TEXT,
                content_text TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week INTEGER NOT NULL,
                pair_number INTEGER NOT NULL,
                time_str TEXT,
                subject_name TEXT NOT NULL,
                classroom TEXT,
                teacher TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                tg_name TEXT,
                student_name TEXT,
                student_group TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_schedules (
                group_name TEXT PRIMARY KEY,
                title TEXT,
                schedule_text TEXT,
                photo_file_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ui_messages (
                user_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verification_ui_messages (
                user_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gdrive_schedule_files (
                file_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                doc_type TEXT,
                schedule_date TEXT,
                part_number INTEGER DEFAULT 1,
                local_path TEXT,
                file_size INTEGER DEFAULT 0,
                drive_timestamp INTEGER DEFAULT 0,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.executescript("""
            CREATE INDEX IF NOT EXISTS idx_subjects_user ON subjects(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_user_subject ON tasks(user_id, subject_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_subject_title ON tasks(subject_id, title);
            CREATE INDEX IF NOT EXISTS idx_materials_task_status ON materials(task_id, status);
            CREATE INDEX IF NOT EXISTS idx_materials_status_id ON materials(status, id);
            CREATE INDEX IF NOT EXISTS idx_users_status_group ON users(status, student_group);
            CREATE INDEX IF NOT EXISTS idx_users_status_name ON users(status, student_name);
        """)
        conn.commit()

def get_ui_message_id(user_id: int) -> Optional[int]:
    """Returns the ID of the user's persistent private-chat menu message."""
    with get_connection() as conn:
        row = conn.execute("SELECT message_id FROM ui_messages WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["message_id"]) if row else None

def set_ui_message_id(user_id: int, message_id: int):
    """Stores the current persistent private-chat menu message ID."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO ui_messages (user_id, message_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, message_id))

def clear_ui_message_id(user_id: int):
    """Forgets a deleted or otherwise unavailable menu message."""
    with get_connection() as conn:
        conn.execute("DELETE FROM ui_messages WHERE user_id = ?", (user_id,))

def get_all_ui_messages() -> List[Dict[str, int]]:
    """Returns the last tracked bot menu for each user for a one-time cleanup."""
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, message_id FROM ui_messages ORDER BY user_id").fetchall()
        return [{"user_id": int(row["user_id"]), "message_id": int(row["message_id"])} for row in rows]

def claim_initial_ui_cleanup() -> bool:
    """Ensures the legacy-menu cleanup runs only once for this database."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('initial_ui_cleanup_done', '1')"
        )
        return cursor.rowcount == 1

def get_verification_ui_message_id(user_id: int) -> Optional[int]:
    """Returns the ID of the dedicated admin verification queue message."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT message_id FROM verification_ui_messages WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["message_id"]) if row else None

def set_verification_ui_message_id(user_id: int, message_id: int):
    """Stores the ID of the dedicated admin verification queue message."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO verification_ui_messages (user_id, message_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, message_id))

def clear_verification_ui_message_id(user_id: int):
    """Forgets a deleted or unavailable admin verification queue message."""
    with get_connection() as conn:
        conn.execute("DELETE FROM verification_ui_messages WHERE user_id = ?", (user_id,))

DEFAULT_SUBJECTS_BY_GROUP = {
    "ТТ-36": [
        "Моделирование",
        "Отделка",
        "ТПМ",
        "Основы дизайна",
        "Автоматизация",
        "Экономика",
        "Предпринимательство",
        "Курсовой по экономике"
    ],
    "ХК": [
        "Компьютерная графика",
        "Прикладное черчение",
        "Народные промыслы",
        "Основы композиции и цветоведения",
        "Основы права",
        "Деловые коммуникации",
        "Основы экономики и предпринимательской деятельности",
        "Рисунок и живопись",
        "Шрифты",
        "Технология художественно-оформительских работ",
        "Технология изделий из керамики",
        "Материаловедение",
        "Прикладная информатика"
    ],
    "ВД": [
        "Методика экологического образования",
        "Природоведение (2 курс)",
        "Психология",
        "Педагогика",
        "Информационные технологии",
        "Методика развития речи",
        "Белорусский язык (проф. лексика) (3 курс)",
        "Современный белорусский язык (2 курс)",
        "Мировая детская литература",
        "Формирование элементарных математических основ",
        "Изобразительная деятельность",
        "История белорусской государственности (3 курс)",
        "ФИЗО",
        "Методика музыкального образования и воспитания детей",
        "Технология инклюзивного образования"
    ]
}

DEFAULT_SUBJECTS = DEFAULT_SUBJECTS_BY_GROUP["ТТ-36"]

def is_xk_group(group_name: Optional[str]) -> bool:
    """Проверяет, относится ли группа к направлению ХК (ХК-37, ХК-38, ХК-39)"""
    if not group_name:
        return False
    return group_name.strip().upper().startswith("ХК")

def group_has_subjects_support(group_name: Optional[str]) -> bool:
    """Проверяет, есть ли для данной группы предзаполненные предметы и методички"""
    if not group_name:
        return False
    grp = group_name.strip().upper()
    return grp == "ТТ-36" or grp.startswith(("ХК", "ВД"))

def get_default_subjects_for_group(group_name: Optional[str]) -> List[str]:
    """Возвращает список предметов семестра по умолчанию для конкретной группы"""
    if not group_name:
        return []
    grp = group_name.strip().upper()
    if grp == "ТТ-36":
        return DEFAULT_SUBJECTS_BY_GROUP["ТТ-36"]
    if grp.startswith("ХК"):
        return DEFAULT_SUBJECTS_BY_GROUP["ХК"]
    if grp.startswith("ВД"):
        return DEFAULT_SUBJECTS_BY_GROUP["ВД"]
    return []

COLLEGE_GROUPS = [
    "ТТ-36",
    "ТТ-37",
    "ТТ-38",
    "ТТЧ-37",
    "ТТЧ-38",
    "ХК-37",
    "ХК-38",
    "ХК-39",
    "ВД-65",
    "ВД-66",
    "ВД-67",
    "ВД-68",
    "ВД-69",
    "ВД-70",
    "С-37",
    "С-38",
    "С-39",
    "СР-38",
]

def seed_default_subjects(user_id: int, student_group: Optional[str] = None):
    """Инициализирует предметы пользователя по умолчанию в зависимости от его группы."""
    if student_group is None:
        user = get_user(user_id)
        student_group = user.get("student_group") if user else None
    admin_id = get_admin_id()
    
    subjects_to_seed = get_default_subjects_for_group(student_group)
    if not subjects_to_seed and (admin_id is not None and user_id == admin_id):
        subjects_to_seed = DEFAULT_SUBJECTS_BY_GROUP["ТТ-36"]

    if not subjects_to_seed:
        return

    # Предметы других групп, которые НЕ должны быть у этого пользователя
    other_default_subjects = set()
    for grp_key, subs in DEFAULT_SUBJECTS_BY_GROUP.items():
        if grp_key != student_group and not (student_group and student_group.strip().upper().startswith(grp_key)):
            other_default_subjects.update(subs)

    with get_connection() as conn:
        cursor = conn.cursor()
        # Удаляем дефолтные предметы чужих групп, если они попали к пользователю
        if other_default_subjects:
            placeholders = ",".join("?" for _ in other_default_subjects)
            cursor.execute(f"""
                DELETE FROM subjects 
                WHERE user_id = ? 
                  AND name IN ({placeholders})
                  AND id NOT IN (SELECT subject_id FROM tasks WHERE user_id = ?)
            """, (user_id, *other_default_subjects, user_id))

        for name in subjects_to_seed:
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO subjects (user_id, name) VALUES (?, ?)", 
                    (user_id, name)
                )
            except sqlite3.IntegrityError:
                pass
        conn.commit()


def get_subjects_with_counts(user_id: int, limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """Возвращает список предметов с количеством долгов и общим числом работ"""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 
                s.id, 
                s.name,
                COUNT(t.id) as total_tasks,
                SUM(CASE WHEN t.is_done = 0 THEN 1 ELSE 0 END) as pending_tasks
            FROM subjects s
            LEFT JOIN tasks t ON s.id = t.subject_id
            WHERE s.user_id = ?
            GROUP BY s.id
            ORDER BY s.name ASC
        """
        if limit is not None:
            cursor.execute(query + " LIMIT ? OFFSET ?", (user_id, max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query, (user_id,))
        rows = cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "total_tasks": row["total_tasks"] or 0,
                "pending_tasks": row["pending_tasks"] or 0
            }
            for row in rows
        ]

def add_subject(user_id: int, name: str) -> Optional[int]:
    """Добавляет предмет. Возвращает id или None, если уже существует"""
    clean_name = name.strip()
    if not clean_name:
        return None
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM subjects WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, clean_name))
            if cursor.fetchone():
                return None
            cursor.execute("INSERT INTO subjects (user_id, name) VALUES (?, ?)", (user_id, clean_name))
            conn.commit()
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None

def delete_subject(user_id: int, subject_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE user_id = ? AND subject_id = ?", (user_id, subject_id))
        cursor.execute("DELETE FROM subjects WHERE user_id = ? AND id = ?", (user_id, subject_id))
        conn.commit()

def get_subject_by_id(user_id: int, subject_id: int) -> Optional[Dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM subjects WHERE user_id = ? AND id = ?", (user_id, subject_id))
        row = cursor.fetchone()
        if row:
            return {"id": row["id"], "name": row["name"]}
        return None

# --- Работы / Задачи ---

def get_tasks_for_subject(user_id: int, subject_id: int, limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """Возвращает работы по предмету (сначала долги, затем сданные), с необязательной страницей."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, title, is_done 
            FROM tasks 
            WHERE user_id = ? AND subject_id = ?
            ORDER BY is_done ASC, id ASC
        """
        if limit is not None:
            cursor.execute(query + " LIMIT ? OFFSET ?", (user_id, subject_id, max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query, (user_id, subject_id))
        rows = cursor.fetchall()
        return [{"id": r["id"], "title": r["title"], "is_done": bool(r["is_done"])} for r in rows]

def get_subject_tasks_page(user_id: int, subject_id: int, limit: int = 25, offset: int = 0) -> Optional[Dict]:
    """Возвращает предмет, общий прогресс и страницу работ за два коротких запроса."""
    with get_connection() as conn:
        subject = conn.execute("""
            SELECT s.id, s.name, COUNT(t.id) AS total_tasks,
                   SUM(CASE WHEN t.is_done = 0 THEN 1 ELSE 0 END) AS pending_tasks
            FROM subjects s
            LEFT JOIN tasks t ON t.subject_id = s.id AND t.user_id = s.user_id
            WHERE s.user_id = ? AND s.id = ?
            GROUP BY s.id
        """, (user_id, subject_id)).fetchone()
        if not subject:
            return None
        tasks = conn.execute("""
            SELECT id, title, is_done
            FROM tasks
            WHERE user_id = ? AND subject_id = ?
            ORDER BY is_done ASC, id ASC
            LIMIT ? OFFSET ?
        """, (user_id, subject_id, max(1, int(limit)), max(0, int(offset)))).fetchall()
        return {
            "subject": {"id": subject["id"], "name": subject["name"]},
            "total": subject["total_tasks"] or 0,
            "pending": subject["pending_tasks"] or 0,
            "tasks": [{"id": r["id"], "title": r["title"], "is_done": bool(r["is_done"])} for r in tasks],
        }

def add_task(user_id: int, subject_id: int, title: str) -> Optional[int]:
    clean_title = title.strip()
    if not clean_title:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM subjects WHERE user_id = ? AND id = ?", (user_id, subject_id))
        if not cursor.fetchone():
            return None
        cursor.execute(
            "INSERT INTO tasks (user_id, subject_id, title, is_done) VALUES (?, ?, ?, 0)",
            (user_id, subject_id, clean_title)
        )
        conn.commit()
        return cursor.lastrowid

def add_tasks(user_id: int, subject_id: int, titles: List[str]) -> int:
    """Добавляет пакет работ одной транзакцией после однократной проверки владения предметом."""
    clean_titles = [title.strip() for title in titles if title and title.strip()]
    if not clean_titles:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM subjects WHERE user_id = ? AND id = ?", (user_id, subject_id))
        if not cursor.fetchone():
            return 0
        cursor.executemany(
            "INSERT INTO tasks (user_id, subject_id, title, is_done) VALUES (?, ?, ?, 0)",
            [(user_id, subject_id, title) for title in clean_titles],
        )
        conn.commit()
        return cursor.rowcount

def toggle_task_status(user_id: int, task_id: int) -> Optional[bool]:
    """Переключает статус работы (0 <-> 1). Возвращает новый статус"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_done, subject_id FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id))
        row = cursor.fetchone()
        if not row:
            return None
        new_status = 1 if row["is_done"] == 0 else 0
        cursor.execute("UPDATE tasks SET is_done = ? WHERE user_id = ? AND id = ?", (new_status, user_id, task_id))
        conn.commit()
        return bool(new_status)

def toggle_task_status_and_get_tasks(
    user_id: int, subject_id: int, task_id: int, limit: int = 25, offset: int = 0
) -> Tuple[Optional[bool], List[Dict], int, int]:
    """Переключает статус и возвращает страницу работ со сводными счётчиками одним соединением."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_done FROM tasks WHERE user_id = ? AND subject_id = ? AND id = ?",
            (user_id, subject_id, task_id),
        )
        row = cursor.fetchone()
        if not row:
            return None, [], 0, 0
        new_status = 0 if row["is_done"] else 1
        cursor.execute(
            "UPDATE tasks SET is_done = ? WHERE user_id = ? AND subject_id = ? AND id = ?",
            (new_status, user_id, subject_id, task_id),
        )
        stats = cursor.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN is_done = 0 THEN 1 ELSE 0 END) AS pending
            FROM tasks WHERE user_id = ? AND subject_id = ?
        """, (user_id, subject_id)).fetchone()
        cursor.execute("""
            SELECT id, title, is_done
            FROM tasks
            WHERE user_id = ? AND subject_id = ?
            ORDER BY is_done ASC, id ASC
            LIMIT ? OFFSET ?
        """, (user_id, subject_id, max(1, int(limit)), max(0, int(offset))))
        tasks = [
            {"id": task["id"], "title": task["title"], "is_done": bool(task["is_done"])}
            for task in cursor.fetchall()
        ]
        conn.commit()
        return bool(new_status), tasks, stats["total"] or 0, stats["pending"] or 0

def delete_task(user_id: int, task_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id))
        conn.commit()

# --- Сводка всех долгов ---

def get_all_debts_summary(user_id: int, include_by_subject: bool = False) -> Dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        # Общие цифры
        cursor.execute("""
            SELECT 
                COUNT(id) as total,
                SUM(CASE WHEN is_done = 1 THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN is_done = 0 THEN 1 ELSE 0 END) as pending
            FROM tasks
            WHERE user_id = ?
        """, (user_id,))
        stats = cursor.fetchone()
        total = stats["total"] or 0
        done = stats["done"] or 0
        pending = stats["pending"] or 0

        by_subject = {}
        if include_by_subject:
            cursor.execute("""
                SELECT s.name as subject_name, t.title as task_title
                FROM tasks t
                JOIN subjects s ON t.subject_id = s.id
                WHERE t.user_id = ? AND t.is_done = 0
                ORDER BY s.name ASC, t.id ASC
            """, (user_id,))
            for row in cursor.fetchall():
                by_subject.setdefault(row["subject_name"], []).append(row["task_title"])

        percent = int((done / total * 100)) if total > 0 else 0
        # Прогресс-бар
        filled = int(percent / 10)
        bar = "█" * filled + "░" * (10 - filled)

        return {
            "total": total,
            "done": done,
            "pending": pending,
            "percent": percent,
            "bar": bar,
            "by_subject": by_subject
        }

def get_pending_tasks_page(user_id: int, limit: int = 25, offset: int = 0) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.id, t.title, s.name AS subject_name
            FROM tasks t
            JOIN subjects s ON s.id = t.subject_id
            WHERE t.user_id = ? AND t.is_done = 0
            ORDER BY s.name COLLATE NOCASE, t.id
            LIMIT ? OFFSET ?
        """, (user_id, max(1, int(limit)), max(0, int(offset)))).fetchall()
        return [dict(row) for row in rows]

# --- Методички и Решения (Материалы) ---

def add_material(user_id: int, user_name: str, task_id: int, material_type: str, title: str, 
                 file_id: Optional[str] = None, file_type: str = "text", content_text: Optional[str] = None) -> Optional[int]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id))
        if not cursor.fetchone():
            return None
        cursor.execute("""
            INSERT INTO materials (user_id, user_name, task_id, material_type, title, file_id, file_type, content_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (user_id, user_name, task_id, material_type, title, file_id, file_type, content_text))
        conn.commit()
        return cursor.lastrowid

def get_approved_materials(task_id: int) -> List[Dict]:
    """Возвращает проверенные методички и решения для работы (общие для всех пользователей!)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, user_name, task_id, material_type, title, file_id, file_type, content_text, created_at
            FROM materials
            WHERE task_id = ? AND status = 'approved'
            ORDER BY material_type DESC, id ASC
        """, (task_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def get_material_by_id(material_id: int) -> Optional[Dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.*, t.title as task_title, s.name as subject_name
            FROM materials m
            LEFT JOIN tasks t ON m.task_id = t.id
            LEFT JOIN subjects s ON t.subject_id = s.id
            WHERE m.id = ?
        """, (material_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def set_material_status(material_id: int, status: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE materials SET status = ? WHERE id = ?", (status, material_id))
        conn.commit()

def count_materials_for_task(task_id: int) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(id) FROM materials WHERE task_id = ? AND status = 'approved'", (task_id,))
        row = cursor.fetchone()
        return row[0] if row else 0

def get_material_counts_for_tasks(task_ids: List[int]) -> Dict[int, int]:
    """Возвращает число одобренных материалов по набору task_id одним запросом."""
    ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT task_id, COUNT(id) AS count FROM materials "
            f"WHERE status = 'approved' AND task_id IN ({placeholders}) GROUP BY task_id",
            ids,
        ).fetchall()
        counts = {row["task_id"]: row["count"] for row in rows}
        return {task_id: counts.get(task_id, 0) for task_id in ids}

def get_global_material_counts_for_subject(subject_name: str) -> Dict[str, int]:
    """Загружает счётчики материалов для всех работ предмета одним запросом."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.title, COUNT(m.id) AS count
            FROM tasks t
            JOIN subjects s ON s.id = t.subject_id
            LEFT JOIN materials m ON m.task_id = t.id AND m.status = 'approved'
            WHERE LOWER(s.name) = LOWER(?)
            GROUP BY LOWER(t.title)
        """, (subject_name.strip(),)).fetchall()
        return {row["title"].strip().lower(): row["count"] for row in rows if row["title"]}

def get_pending_materials(limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """Возвращает страницу материалов, ожидающих верификации администратором."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT m.*, t.title as task_title, s.name as subject_name
            FROM materials m
            JOIN tasks t ON m.task_id = t.id
            JOIN subjects s ON t.subject_id = s.id
            WHERE m.status = 'pending'
            ORDER BY m.id ASC
        """
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            cursor.execute(query, (max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def count_pending_materials() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM materials WHERE status = 'pending'").fetchone()[0]

def get_task_info(task_id: int) -> Optional[Dict]:
    """Возвращает информацию о работе и предмете"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.id, t.title, t.is_done, s.id as subject_id, s.name as subject_name
            FROM tasks t
            JOIN subjects s ON t.subject_id = s.id
            WHERE t.id = ?
        """, (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_task_info_for_subject(user_id: int, subject_id: int, task_id: int) -> Optional[Dict]:
    """Looks up a task only when it belongs to the requesting user's subject."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT t.id, t.user_id, t.subject_id, t.title, s.name AS subject_name
            FROM tasks t JOIN subjects s ON s.id = t.subject_id AND s.user_id = t.user_id
            WHERE t.user_id = ? AND t.subject_id = ? AND t.id = ?
        """, (user_id, subject_id, task_id)).fetchone()
        return dict(row) if row else None

# --- База знаний (Материалы по всей группе) ---

def get_global_material_counts_by_subject() -> Dict[str, int]:
    """Возвращает словарь {subject_name_lower: count_approved_materials}"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.name as sub_name, COUNT(m.id) as count
            FROM materials m
            JOIN tasks t ON m.task_id = t.id
            JOIN subjects s ON t.subject_id = s.id
            WHERE m.status = 'approved'
            GROUP BY LOWER(s.name)
        """)
        rows = cursor.fetchall()
        return {r["sub_name"].strip().lower(): r["count"] for r in rows if r["sub_name"]}

def get_all_known_subject_names(user_id: Optional[int] = None) -> List[str]:
    """Возвращает список всех известных предметов (дефолтные для группы пользователя + созданные пользователями)"""
    group_name = None
    if user_id:
        user = get_user(user_id)
        group_name = user.get("student_group") if user else None

    with get_connection() as conn:
        cursor = conn.cursor()
        if user_id and group_name:
            cursor.execute("""
                SELECT DISTINCT s.name 
                FROM subjects s
                JOIN users u ON s.user_id = u.user_id
                WHERE u.student_group = ? OR s.user_id = ?
                ORDER BY s.name ASC
            """, (group_name, user_id))
            db_subs = [r["name"] for r in cursor.fetchall()]
        else:
            cursor.execute("SELECT DISTINCT name FROM subjects ORDER BY name ASC")
            db_subs = [r["name"] for r in cursor.fetchall()]
        
    seen = set()
    result = []
    forbidden_other = set()

    if group_name:
        default_list = get_default_subjects_for_group(group_name)
        # Исключаем дефолтные предметы других групп, чтобы ХК не смешивался с ТТ-36
        for g_k, g_subs in DEFAULT_SUBJECTS_BY_GROUP.items():
            if g_k != group_name and not group_name.strip().upper().startswith(g_k):
                forbidden_other.update(item.strip().lower() for item in g_subs)
    else:
        default_list = []
        for g_subs in DEFAULT_SUBJECTS_BY_GROUP.values():
            default_list.extend(g_subs)

    for name in default_list:
        s = name.strip()
        if s.lower() not in seen and s.lower() not in forbidden_other:
            seen.add(s.lower())
            result.append(s)

    for name in db_subs:
        s = name.strip()
        if s.lower() not in seen and s.lower() not in forbidden_other:
            seen.add(s.lower())
            result.append(s)

    return result

def get_approved_materials_by_subject_name(subject_name: str) -> List[Dict]:
    """Возвращает все проверенные материалы по названию предмета (общие для всей группы)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.id, m.user_id, m.user_name, m.task_id, m.material_type, 
                   m.title, m.file_id, m.file_type, m.content_text, m.created_at,
                   t.title as task_title, s.name as subject_name
            FROM materials m
            JOIN tasks t ON m.task_id = t.id
            JOIN subjects s ON t.subject_id = s.id
            WHERE LOWER(s.name) = LOWER(?) AND m.status = 'approved'
            ORDER BY t.title ASC, m.material_type DESC, m.id ASC
        """, (subject_name.strip(),))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def get_materials_for_task_globally(
    subject_name: str, task_title: str, limit: Optional[int] = None, offset: int = 0
) -> List[Dict]:
    """Возвращает материалы по предмету и работе, с необязательной страницей."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT m.id, m.user_id, m.user_name, m.task_id, m.material_type, 
                   m.title, m.file_id, m.file_type, m.content_text, m.created_at,
                   t.title as task_title, s.name as subject_name
            FROM materials m
            JOIN tasks t ON m.task_id = t.id
            JOIN subjects s ON t.subject_id = s.id
            WHERE LOWER(s.name) = LOWER(?) AND LOWER(t.title) = LOWER(?) AND m.status = 'approved'
            ORDER BY m.material_type DESC, m.id ASC
        """
        params = (subject_name.strip(), task_title.strip())
        if limit is not None:
            cursor.execute(query + " LIMIT ? OFFSET ?", (*params, max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def count_materials_for_task_globally(subject_name: str, task_title: str) -> int:
    """Считает материалы по названию предмета и названию работы"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(m.id)
            FROM materials m
            JOIN tasks t ON m.task_id = t.id
            JOIN subjects s ON t.subject_id = s.id
            WHERE LOWER(s.name) = LOWER(?) AND LOWER(t.title) = LOWER(?) AND m.status = 'approved'
        """, (subject_name.strip(), task_title.strip()))
        row = cursor.fetchone()
        return row[0] if row else 0

def delete_material(material_id: int) -> bool:
    """Удаляет неактуальный материал из базы данных навсегда"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM materials WHERE id = ?", (material_id,))
        conn.commit()
        return cursor.rowcount > 0

# --- Настройки и Администратор ---

def get_admin_id() -> Optional[int]:
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = 'admin_id'")
            row = cursor.fetchone()
            if row and row["value"]:
                return int(row["value"])
    except Exception:
        return None
    return None

def set_admin_id(admin_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_id', ?)", (str(admin_id),))
        conn.commit()

# --- Расписание занятий (Пятнашка) ---

DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SHORT_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

DEFAULT_PAIR_TIMES = {
    0: "08:15 – 09:00",
    1: "09:10 – 10:50",
    2: "11:00 – 13:20",
    3: "13:30 – 15:10",
    4: "15:20 – 17:00",
    5: "17:10 – 18:50",
    6: "19:00 – 20:40",
}

BELLS_SCHEDULE_TEXT = (
    "🔔 <b>РАСПИСАНИЕ ЗВОНКОВ (2026/2027 уч. год)</b> 🎓\n"
    "<i>УО «Бобруйский государственный колледж им. А.Е. Ларина»</i>\n\n"
    "📅 <b>ПОНЕДЕЛЬНИК – ПЯТНИЦА:</b>\n"
    "0️⃣ урок: <b>08:15 – 09:00</b> <i>(Пн — кураторский, Чт — информ.)</i>\n"
    "1️⃣ урок: <b>09:10 – 09:55</b>\n"
    "2️⃣ урок: <b>10:05 – 10:50</b>\n"
    "3️⃣ урок: <b>11:00 – 11:45</b>\n"
    "🍽️ <b>ОБЕД: 11:45 – 12:35</b> (50 мин)\n"
    "4️⃣ урок: <b>12:35 – 13:20</b>\n"
    "5️⃣ урок: <b>13:30 – 14:15</b>\n"
    "6️⃣ урок: <b>14:25 – 15:10</b>\n"
    "7️⃣ урок: <b>15:20 – 16:05</b>\n"
    "8️⃣ урок: <b>16:15 – 17:00</b>\n"
    "9️⃣ урок: <b>17:10 – 17:55</b>\n"
    "🔟 урок: <b>18:05 – 18:50</b>\n"
    "1️⃣1️⃣ урок: <b>19:00 – 19:45</b>\n"
    "1️⃣2️⃣ урок: <b>19:55 – 20:40</b>\n\n"
    "📅 <b>СУББОТА:</b>\n"
    "1️⃣ урок: <b>08:15 – 09:00</b>\n"
    "2️⃣ урок: <b>09:10 – 09:55</b>\n"
    "3️⃣ урок: <b>10:05 – 10:50</b>\n"
    "4️⃣ урок: <b>11:00 – 11:45</b>\n"
    "🍽️ <b>ОБЕД: 11:45 – 12:10</b> (25 мин)\n"
    "5️⃣ урок: <b>12:10 – 12:55</b>\n"
    "6️⃣ урок: <b>13:05 – 13:50</b>\n"
    "7️⃣ урок: <b>14:00 – 14:45</b>\n"
    "8️⃣ урок: <b>14:55 – 15:40</b>\n"
    "9️⃣ урок: <b>15:50 – 16:35</b>"
)

# Расписания по группам колледжа им. А.Е. Ларина (взяты из официальных листов замен Google Drive)
DEFAULT_GROUP_SCHEDULES = {
    "ТТ-36": {
        "title": "Расписание занятий (ТТ-36)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Валенчиц Т.А., каб. 11)\n"
            "1️⃣ 09:10 – 10:50 — «а» Технология отделки мебели и столярных изделий ЛПЗ (Чванькова Н.В., каб. 15) / «б» Экономика организации КР (Шиенок Т.Г., каб. 20)\n"
            "2️⃣ 11:00 – 13:20 — «а» Экономика организации КР (Шиенок Т.Г., каб. 20) / «б» Основы дизайна мебели ЛПЗ (Чернышев Е.А., каб. 32)\n"
            "3️⃣ 13:30 – 15:10 — Технология производства мебели (Валенчиц Т.А., каб. 11)"
        )
    },
    "ТТ-37": {
        "title": "Расписание занятий (ТТ-37)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Чернышев Е.А., каб. 32)\n"
            "1️⃣ 09:10 – 10:50 — «а» Основы проектной графики ЛПЗ (Чернышев Е.А., каб. 32) / «б» Технология д/о производств ЛПЗ (Валенчиц Т.А., каб. 11)\n"
            "2️⃣ 11:00 – 13:20 — Технология производства мебели ЛПЗ (Валенчиц Т.А. / Чванькова Н.В., каб. 11/15)\n"
            "3️⃣ 13:30 – 15:10 — Оборудование д/о производств (Чванькова Н.В., каб. 15)"
        )
    },
    "ТТ-38": {
        "title": "Расписание занятий (ТТ-38)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Нестерович О.С., каб. 10)\n"
            "1️⃣ 09:10 – 09:55 — Техническая механика (Миткевич Е.А., каб. 9)\n"
            "2️⃣ 10:05 – 11:45 — «а» Техническая механика ЛПЗ (Миткевич Е.А., каб. 9) / «б» Сушка и защита древесины ЛПЗ (Кашевич А.Ю., каб. 16)\n"
            "3️⃣ 11:00 – 13:20 — «а» Сушка и защита древесины ЛПЗ (Кашевич А.Ю., каб. 16) / «б» Техническая механика ЛПЗ (Миткевич Е.А., каб. 9)\n"
            "4️⃣ 14:25 – 15:10 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)\n"
            "5️⃣ 15:20 – 17:00 — Древесиноведение ЛПЗ (Чванькова Н.В. / Чернышев Е.А., каб. 15/32)"
        )
    },
    "ТТЧ-37": {
        "title": "Расписание занятий (ТТЧ-37)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Гордейчик М.Н., каб. 5)\n"
            "1️⃣ 10:05 – 11:45 — «а» Художественный ручной труд (Бобков Н.Г., каб. 31) / «б» Художественная обработка материалов (Павликов А.С., каб. 12)\n"
            "2️⃣ 12:35 – 13:20 — Методика трудового обучения (Бобков Н.Г., каб. 20)\n"
            "3️⃣ 13:30 – 15:10 — Методика обучения черчению (Киселева Н.Н., каб. 23)\n"
            "4️⃣ 15:20 – 16:05 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)"
        )
    },
    "ТТЧ-38": {
        "title": "Расписание занятий (ТТЧ-38)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Снят\n"
            "1️⃣ 09:10 – 09:55 — Математика (Жаркова Н.А., каб. 1)\n"
            "2️⃣ 10:05 – 10:50 — Иностранный язык (Богданович И.В. / Козловский Д.Ю., каб. 28/29)\n"
            "3️⃣ 11:00 – 11:45 — Кураторский час (Федорцова Т.Л., каб. 4)\n"
            "4️⃣ 12:35 – 13:20 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)\n"
            "5️⃣ 13:30 – 14:15 — Информатика (Горбач Л.В. / Р.Павлюк И.А., каб. 7/8)\n"
            "6️⃣ 14:25 – 15:10 — История Беларуси в контексте всемирной истории (Гордейчик М.Н., каб. 5)\n"
            "7️⃣ 15:20 – 16:05 — Русская литература (Родионова Т.Ф., каб. 10)"
        )
    },
    "ХК-37": {
        "title": "Расписание занятий (ХК-37)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Котлярова Ж.Ю., каб. 22)\n"
            "1️⃣ 09:10 – 10:50 — «а» Компьютерная графика (Горбач Л.В., каб. 7) / «б» Рисунок и живопись (Киселева Н.Н., каб. 24)\n"
            "2️⃣ 11:00 – 13:20 — «а» Рисунок и живопись (Котлярова Ж.Ю., каб. 22) / «б» Компьютерная графика (Горбач Л.В., каб. 7)\n"
            "3️⃣ 13:30 – 14:15 — Основы права (Гордейчик М.Н., каб. 5)\n"
            "4️⃣ 14:25 – 15:10 — Физическая культура и здоровье (Шип А.В., с/з)"
        )
    },
    "ХК-38": {
        "title": "Расписание занятий (ХК-38)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Физическая культура и здоровье (Барлюгова В.В., с/з)\n"
            "1️⃣ 09:10 – 09:55 — Материаловедение (Самусева Т.В., УПМ-13)\n"
            "2️⃣ 10:05 – 10:50 — Технология изготовления художественных изделий из керамики (Самусева Т.В., УПМ-13)\n"
            "3️⃣ 11:00 – 11:45 — Пластическая анатомия (Киселева Н.Н., каб. 23)\n"
            "4️⃣ 12:35 – 13:20 — Технология художественно-оформительских работ (Киселева Н.Н., каб. 23)\n"
            "5️⃣ 13:30 – 14:15 — Химия (Бондарович Н.А., каб. 2)\n"
            "6️⃣ 14:25 – 15:10 — Русский язык (Родионова Т.Ф., каб. 10)"
        )
    },
    "ХК-39": {
        "title": "Расписание занятий (ХК-39)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Киселева Н.Н., каб. 24)\n"
            "1️⃣ 09:10 – 09:55 — Биология (Кражов А.А., каб. 6)\n"
            "2️⃣ 10:05 – 10:50 — Белорусская литература (Федорцова Т.Л., каб. 4)\n"
            "3️⃣ 11:00 – 13:20 — «а» Иностранный язык (Богданович И.В., каб. 28) / «б» Лепка (Самусева Т.В., УПМ-13)\n"
            "4️⃣ 13:30 – 14:15 — Математика (Жаркова Н.А., каб. 1)\n"
            "5️⃣ 14:25 – 16:05 — «а» Снят / «б» Иностранный язык (Козловский Д.Ю., каб. 29)"
        )
    },
    "ВД-65": {
        "title": "Расписание занятий (ВД-65)",
        "text": (
            "0️⃣ – 2️⃣ уроки — Сняты\n"
            "1️⃣ 11:00 – 13:20 — Педагогика (Стельмак Е.В., каб. 19)\n"
            "2️⃣ 13:30 – 15:10 — «а» Декоративно-прикладное искусство (Котлярова Ж.Ю., каб. 22) / «б» Музыка и методика музыкального воспитания (Пушнова Г.В., каб. 18)\n"
            "3️⃣ 15:20 – 17:00 — Мировая детская литература (Стельмак Е.В., каб. 19)\n"
            "4️⃣ 17:10 – 17:55 — Кураторский час (Горбач Л.В., каб. 7)"
        )
    },
    "ВД-66": {
        "title": "Расписание занятий (ВД-66)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Бондарович Н.А., каб. 2)\n"
            "1️⃣ 09:10 – 10:50 — Педагогика (Стельмак Е.В., каб. 19)\n"
            "2️⃣ 11:00 – 13:20 — Технология инклюзивного образования (Зенькович Н.М., каб. 26)\n"
            "3️⃣ 13:30 – 15:10 — Мировая детская литература (Стельмак Е.В., каб. 19)"
        )
    },
    "ВД-67": {
        "title": "Расписание занятий (ВД-67)",
        "text": (
            "0️⃣ – 2️⃣ уроки — Сняты\n"
            "1️⃣ 11:00 – 11:45 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)\n"
            "2️⃣ 12:35 – 13:20 — Основы экономики образования (Кражов А.А., каб. 6)\n"
            "3️⃣ 13:30 – 14:15 — Культура речи педагога (Воробьева А.В., каб. 21)\n"
            "4️⃣ 14:25 – 15:10 — Информационные технологии (Горбач Л.В. / Р.Павлюк И.А., каб. 7/8)\n"
            "5️⃣ 15:20 – 17:00 — Основы права (Гордейчик М.Н., каб. 5)"
        )
    },
    "ВД-68": {
        "title": "Расписание занятий (ВД-68)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Сняты\n"
            "1️⃣ 09:10 – 09:55 — Основы права (Гордейчик М.Н., каб. 5)\n"
            "2️⃣ 10:05 – 10:50 — Анатомия, физиология и гигиена с основами мед. знаний (Бондарович Н.А., каб. 2)\n"
            "3️⃣ 11:00 – 13:20 — Психология ЛПЗ (Цмыг А.В. / Пушнова Г.В., каб. 21/18)\n"
            "4️⃣ 13:30 – 15:10 — Педагогика (Мороз В.М., каб. 20)\n"
            "5️⃣ 15:20 – 16:05 — Кураторский час (Пушнова Г.В., каб. 18)"
        )
    },
    "ВД-69": {
        "title": "Расписание занятий (ВД-69)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Снят\n"
            "1️⃣ 09:10 – 09:55 — Русская литература (Родионова Т.Ф., каб. 10)\n"
            "2️⃣ 10:05 – 10:50 — Математика (Жаркова Н.А., каб. 1)\n"
            "3️⃣ 11:00 – 13:20 — Химия (Бондарович Н.А., каб. 2)\n"
            "4️⃣ 13:30 – 14:15 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)\n"
            "5️⃣ 14:25 – 15:10 — Биология (Бондарович Н.А., каб. 2)\n"
            "6️⃣ 15:20 – 16:05 — Кураторский час (Воробьева А.В., каб. 21)"
        )
    },
    "ВД-70": {
        "title": "Расписание занятий (ВД-70)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Стельмак Е.В., каб. 19)\n"
            "1️⃣ 09:10 – 09:55 — Иностранный язык (Богданович И.В. / Козловский Д.Ю., каб. 28/29)\n"
            "2️⃣ 10:05 – 10:50 — Физическая культура и здоровье (Гусева А.В. / Барлюгова В.В., с/з)\n"
            "3️⃣ 11:00 – 11:45 — Математика (Жаркова Н.А., каб. 1)\n"
            "4️⃣ 12:35 – 13:20 — Русская литература (Родионова Т.Ф., каб. 10)\n"
            "5️⃣ 13:30 – 14:15 — Медицинская подготовка (Зенькович Н.М., каб. 26)\n"
            "6️⃣ 14:25 – 15:10 — Белорусская литература (Федорцова Т.Л., каб. 4)"
        )
    },
    "С-37": {
        "title": "Расписание занятий (С-37)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Кражов А.А., каб. 6)\n"
            "1️⃣ 09:10 – 10:50 — Защита населения и территорий от ЧС (Епишин В.И., каб. 26)\n"
            "2️⃣ 11:00 – 13:20 — Физическая культура и здоровье (Шип А.В., с/з)\n"
            "3️⃣ 13:30 – 15:10 — Конструирование столярных изделий с основами дизайна (Чернышев Е.А., каб. 32)\n"
            "4️⃣ 15:20 – 17:00 — «а» Снят / «б» Прикладная информатика (Горбач Л.В., каб. 7)"
        )
    },
    "С-38": {
        "title": "Расписание занятий (С-38)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Жаркова Н.А., каб. 1)\n"
            "1️⃣ 09:10 – 09:55 — Химия (Бондарович Н.А., каб. 2)\n"
            "2️⃣ 10:05 – 10:50 — История Беларуси в контексте всемирной истории (Гордейчик М.Н., каб. 5)\n"
            "3️⃣ 11:00 – 11:45 — Русский язык (Родионова Т.Ф., каб. 10)\n"
            "4️⃣ 12:35 – 13:20 — Белорусская литература (Федорцова Т.Л., каб. 4)\n"
            "5️⃣ 13:30 – 14:15 — География (Кражов А.А., каб. 6)\n"
            "6️⃣ 14:25 – 15:10 — Математика (Жаркова Н.А., каб. 1)\n"
            "7️⃣ 15:20 – 16:05 — Физическая культура и здоровье (Шип А.В., с/з)"
        )
    },
    "С-39": {
        "title": "Расписание занятий (С-39)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Гусева А.В., каб. 9)\n"
            "1️⃣ 09:10 – 09:55 — Белорусский язык (Федорцова Т.Л., каб. 4)\n"
            "2️⃣ 10:05 – 10:50 — География (Кражов А.А., каб. 6)\n"
            "3️⃣ 11:00 – 11:45 — История Беларуси в контексте всемирной истории (Гордейчик М.Н., каб. 5)\n"
            "4️⃣ 12:35 – 13:20 — Математика (Жаркова Н.А., каб. 1)\n"
            "5️⃣ 13:30 – 14:15 — Русская литература (Родионова Т.Ф., каб. 10)\n"
            "6️⃣ 14:25 – 15:10 — Физика (Миткевич Е.А., каб. 9)\n"
            "7️⃣ 15:20 – 16:05 — Математика (Жаркова Н.А., каб. 1)"
        )
    },
    "СР-38": {
        "title": "Расписание занятий (СР-38)",
        "text": (
            "0️⃣ 08:15 – 09:00 — Кураторский час (Чкалова А.В., каб. 16)\n"
            "1️⃣ 09:10 – 09:55 — Физическая культура и здоровье (Гусева А.В., с/з)\n"
            "2️⃣ 10:05 – 10:50 — Русский язык (Родионова Т.Ф., каб. 10)\n"
            "3️⃣ 11:00 – 11:45 — Допризывная подготовка (Епишин В.И., каб. 6)\n"
            "4️⃣ 12:35 – 13:20 — История Беларуси в контексте всемирной истории (Гордейчик М.Н., каб. 5)\n"
            "5️⃣ 13:30 – 14:15 — Белорусский язык (Федорцова Т.Л., каб. 4)\n"
            "6️⃣ 14:25 – 15:10 — Биология (Кражов А.А., каб. 6)"
        )
    }
}

def get_schedule_for_day(day_of_week: int) -> List[Dict]:
    """Возвращает список пар на день (0=Пн ... 5=Сб, 6=Вс)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM schedule 
            WHERE day_of_week = ? 
            ORDER BY pair_number ASC
        """, (day_of_week,))
        return [dict(r) for r in cursor.fetchall()]

def get_full_week_schedule() -> Dict[int, List[Dict]]:
    """Возвращает расписание на всю неделю {day_int: [pairs]}"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedule ORDER BY day_of_week ASC, pair_number ASC")
        rows = [dict(r) for r in cursor.fetchall()]
        res = {i: [] for i in range(7)}
        for r in rows:
            if r["day_of_week"] in res:
                res[r["day_of_week"]].append(r)
        return res

def add_schedule_pair(day_of_week: int, pair_number: int, subject_name: str, 
                      time_str: Optional[str] = None, classroom: Optional[str] = None, 
                      teacher: Optional[str] = None):
    """Добавляет пару в расписание"""
    time_val = time_str or DEFAULT_PAIR_TIMES.get(pair_number, "")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO schedule (day_of_week, pair_number, time_str, subject_name, classroom, teacher)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (day_of_week, pair_number, time_val, subject_name, classroom, teacher))
        conn.commit()

def clear_schedule_day(day_of_week: int):
    """Очищает расписание на конкретный день"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedule WHERE day_of_week = ?", (day_of_week,))
        conn.commit()

# --- Актуальное оперативное расписание по группам (Пятнашка) ---

def set_group_schedule(group_name: str, schedule_text: str, title: Optional[str] = None, 
                       photo_file_id: Optional[str] = None, updated_by: Optional[str] = None):
    """Сохраняет актуальное расписание для группы (или ALL для всех)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO group_schedules (group_name, title, schedule_text, photo_file_id, updated_at, updated_by)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """, (group_name, title or f"Расписание ({group_name})", schedule_text, photo_file_id, updated_by))
        conn.commit()

def get_group_schedule(group_name: str) -> Optional[Dict]:
    """Возвращает расписание, скрывая устаревшие автоматически засеянные шаблоны."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM group_schedules WHERE group_name = ?", (group_name,))
        row = cursor.fetchone()
        if not row:
            return None
        schedule = dict(row)
        seeded = next((item for item in DEFAULT_GROUP_SCHEDULES.get(group_name, {}).values()
                       if item == schedule.get("schedule_text")), None)
        is_seeded = schedule.get("updated_by") == "Автоматический шаблон" or (
            schedule.get("updated_by") == "Администрация" and seeded is not None
        )
        if is_seeded:
            try:
                updated_at = datetime.fromisoformat(str(schedule.get("updated_at", "")).replace("Z", "+00:00"))
                if updated_at.tzinfo:
                    updated_at = updated_at.replace(tzinfo=None)
                if datetime.utcnow() - updated_at > timedelta(days=DEFAULT_SCHEDULE_MAX_AGE_DAYS):
                    return None
            except (TypeError, ValueError):
                return None
        return schedule

def clear_group_schedule(group_name: str):
    """Очищает расписание для указанной группы"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM group_schedules WHERE group_name = ?", (group_name,))
        conn.commit()

def get_all_group_schedules() -> List[Dict]:
    """Возвращает все группы с активным расписанием"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM group_schedules ORDER BY group_name ASC")
        return [dict(r) for r in cursor.fetchall()]

def get_verified_users_by_group(group_name: str) -> List[Dict]:
    """Возвращает верифицированных студентов конкретной группы"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE status = 'approved' AND student_group = ?", (group_name,))
        return [dict(r) for r in cursor.fetchall()]

def update_user_group(user_id: int, new_group: str):
    """Обновляет учебную группу пользователя"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET student_group = ? WHERE user_id = ?", (new_group, user_id))
        conn.commit()

def seed_default_group_schedule(force_refresh: bool = False):
    """Добавляет локальные шаблоны расписания, если администратор ещё ничего не публиковал."""
    with get_connection() as conn:
        cursor = conn.cursor()
        for grp, info in DEFAULT_GROUP_SCHEDULES.items():
            cursor.execute("SELECT group_name, schedule_text FROM group_schedules WHERE group_name = ?", (grp,))
            row = cursor.fetchone()
            if not row or force_refresh:
                cursor.execute("""
                    INSERT OR REPLACE INTO group_schedules (group_name, title, schedule_text, updated_at, updated_by)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'Автоматический шаблон')
                """, (grp, info["title"], info["text"]))
        conn.commit()

# --- Верификация студентов и пользователей ---

def get_user(user_id: int) -> Optional[Dict]:
    """Возвращает информацию о пользователе из базы"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def is_user_verified(user_id: int) -> bool:
    """Проверяет, одобрен ли доступ пользователю"""
    user = get_user(user_id)
    return user is not None and user.get("status") == "approved"

def add_user_request(user_id: int, username: Optional[str], tg_name: str, student_name: str, student_group: str):
    """Creates a verification request once, or lets a rejected user reapply."""
    clean_uname = f"@{username.lstrip('@')}" if username else None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, username, tg_name, student_name, student_group, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                tg_name = excluded.tg_name,
                student_name = excluded.student_name,
                student_group = excluded.student_group,
                status = 'pending',
                created_at = CURRENT_TIMESTAMP
            WHERE users.status = 'rejected'
        """, (user_id, clean_uname, tg_name, student_name, student_group))
        conn.commit()
        return cursor.rowcount == 1

def set_user_status(user_id: int, status: str):
    """Меняет статус пользователя ('approved', 'rejected')"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users 
            SET status = ?, verified_at = CURRENT_TIMESTAMP 
            WHERE user_id = ?
        """, (status, user_id))
        conn.commit()

def get_pending_users(limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """Возвращает страницу студентов, ожидающих верификации."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM users WHERE status = 'pending' ORDER BY created_at ASC, user_id ASC"
        if limit is not None:
            cursor.execute(query + " LIMIT ? OFFSET ?", (max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query)
        return [dict(r) for r in cursor.fetchall()]

def count_pending_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0]

def get_verified_users(limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
    """Возвращает одобренных студентов, с необязательной постраничной выборкой."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM users WHERE status = 'approved' ORDER BY student_name ASC, user_id ASC"
        if limit is not None:
            cursor.execute(query + " LIMIT ? OFFSET ?", (max(1, int(limit)), max(0, int(offset))))
        else:
            cursor.execute(query)
        return [dict(r) for r in cursor.fetchall()]

def count_verified_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE status = 'approved'").fetchone()[0]

def ensure_admin_user(admin_id: int, username: str = "Menmamoh"):
    """Гарантирует, что админ @Menmamoh всегда имеет approved статус, без навязывания группы"""
    clean_uname = f"@{username.lstrip('@')}"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_group FROM users WHERE user_id = ?", (admin_id,))
        row = cursor.fetchone()
        existing_group = row["student_group"] if row and row["student_group"] else None
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, username, tg_name, student_name, student_group, status, verified_at)
            VALUES (?, ?, 'Администратор', 'Администратор', ?, 'approved', CURRENT_TIMESTAMP)
        """, (admin_id, clean_uname, existing_group))
        conn.commit()

def reset_all_verifications(keep_admin_id: Optional[int] = None) -> int:
    """Удаляет всех пользователей (кроме администратора) из базы верификации.
    Сбрасывает их данные до начального состояния."""
    if keep_admin_id is None:
        keep_admin_id = get_admin_id()

    with get_connection() as conn:
        cursor = conn.cursor()
        if keep_admin_id:
            cursor.execute("DELETE FROM users WHERE user_id != ?", (keep_admin_id,))
            deleted = cursor.rowcount
            cursor.execute("DELETE FROM materials WHERE user_id != ?", (keep_admin_id,))
            cursor.execute("DELETE FROM tasks WHERE user_id != ?", (keep_admin_id,))
            cursor.execute("DELETE FROM subjects WHERE user_id != ?", (keep_admin_id,))
        else:
            cursor.execute("DELETE FROM users")
            deleted = cursor.rowcount
            cursor.execute("DELETE FROM materials")
            cursor.execute("DELETE FROM tasks")
            cursor.execute("DELETE FROM subjects")
        conn.commit()
        return deleted

# --- Файлы расписания из Google Диска ---

def save_gdrive_file(file_id: str, filename: str, doc_type: str, schedule_date: Optional[str],
                     part_number: int = 1, local_path: Optional[str] = None,
                     file_size: int = 0, drive_timestamp: int = 0):
    """Сохраняет или обновляет запись о файле из Google Диска"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO gdrive_schedule_files 
            (file_id, filename, doc_type, schedule_date, part_number, local_path, file_size, drive_timestamp, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (file_id, filename, doc_type, schedule_date, part_number, local_path, file_size, drive_timestamp))
        conn.commit()

def get_gdrive_file(file_id: str) -> Optional[Dict]:
    """Возвращает информацию о файле по file_id"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM gdrive_schedule_files WHERE file_id = ?", (file_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_all_gdrive_files() -> List[Dict]:
    """Возвращает все сохранённые файлы из Google Диска"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM gdrive_schedule_files ORDER BY schedule_date DESC, part_number ASC, downloaded_at DESC")
        return [dict(r) for r in cursor.fetchall()]

def get_latest_gdrive_schedule_files(doc_type: Optional[str] = None, min_date: Optional[str] = None) -> List[Dict]:
    """Возвращает файлы последней активной даты расписания (с фильтрацией min_date при необходимости)"""
    if min_date:
        return get_upcoming_gdrive_schedule_files(min_date=min_date, doc_type=doc_type)

    with get_connection() as conn:
        cursor = conn.cursor()
        if doc_type:
            cursor.execute("SELECT MAX(schedule_date) as max_date FROM gdrive_schedule_files WHERE doc_type = ? AND local_path IS NOT NULL", (doc_type,))
        else:
            cursor.execute("SELECT MAX(schedule_date) as max_date FROM gdrive_schedule_files WHERE local_path IS NOT NULL")
        row = cursor.fetchone()
        max_date = row["max_date"] if row else None
        if not max_date:
            return []
        
        if doc_type:
            cursor.execute("""
                SELECT * FROM gdrive_schedule_files 
                WHERE schedule_date = ? AND doc_type = ? AND local_path IS NOT NULL
                ORDER BY part_number ASC
            """, (max_date, doc_type))
        else:
            cursor.execute("""
                SELECT * FROM gdrive_schedule_files 
                WHERE schedule_date = ? AND local_path IS NOT NULL
                ORDER BY part_number ASC
            """, (max_date,))
        return [dict(r) for r in cursor.fetchall()]

def get_gdrive_schedule_files_for_date(schedule_date: str, doc_type: Optional[str] = None) -> List[Dict]:
    """Возвращает файлы для конкретной даты расписания (YYYY-MM-DD)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if doc_type:
            cursor.execute("""
                SELECT * FROM gdrive_schedule_files 
                WHERE schedule_date = ? AND doc_type = ? AND local_path IS NOT NULL
                ORDER BY part_number ASC
            """, (schedule_date, doc_type))
        else:
            cursor.execute("""
                SELECT * FROM gdrive_schedule_files 
                WHERE schedule_date = ? AND local_path IS NOT NULL
                ORDER BY part_number ASC
            """, (schedule_date,))
        return [dict(r) for r in cursor.fetchall()]

def get_upcoming_gdrive_schedule_files(min_date: str, doc_type: Optional[str] = "replacement") -> List[Dict]:
    """Возвращает файлы для ближайшей актуальной даты >= min_date (прошедшие дни игнорируются)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if doc_type:
            cursor.execute("""
                SELECT MIN(schedule_date) as nearest_date 
                FROM gdrive_schedule_files 
                WHERE schedule_date >= ? AND doc_type = ? AND local_path IS NOT NULL
            """, (min_date, doc_type))
        else:
            cursor.execute("""
                SELECT MIN(schedule_date) as nearest_date 
                FROM gdrive_schedule_files 
                WHERE schedule_date >= ? AND local_path IS NOT NULL
            """, (min_date,))
        row = cursor.fetchone()
        nearest = row["nearest_date"] if row else None
        if not nearest:
            return []
        
        return get_gdrive_schedule_files_for_date(nearest, doc_type)




