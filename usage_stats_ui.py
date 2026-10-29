"""Compact Telegram statistics screens. No database writes or network calls."""
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SECTIONS = {
    "schedule": ("📅", "Расписание"), "study": ("📝", "Мои работы"),
    "materials": ("📚", "Материалы"), "useful": ("💡", "Полезное"),
    "profile": ("👤", "Профиль"),
}
PAGE_SIZE = 8


def period_label(days):
    return "Сегодня" if days == 0 else f"За {days} дней"


def clean(value, limit=64):
    return html.escape(str(value or "")[:limit])


def name_of(user):
    return user.get("student_name") or user.get("tg_name") or "Студент"


def identity(user):
    username = user.get("username")
    return clean("@" + username.lstrip("@"), 40) if username else f"ID {int(user['user_id'])}"


def when(value):
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo("Europe/Minsk")).strftime("%d.%m · %H:%M:%S")


def button(label, data):
    return InlineKeyboardButton(text=label, callback_data=data)


def periods(days, target):
    return [button(("✓ " if value == days else "") + label, target(value))
            for value, label in [(0, "Сегодня"), (7, "7 дней"), (30, "30 дней")]]


def pagination(page, total, size, target):
    rows = []
    nav = []
    if page > 0:
        nav.append(button("← Назад", target(page - 1)))
    if (page + 1) * size < total:
        nav.append(button("Далее →", target(page + 1)))
    if nav:
        rows.append(nav)
    return rows


def footer(days, refresh, back=None):
    return [[button("Обновить", refresh), button("К статистике", f"adm_stats_{days}")],
            *([[button("← К людям", back)]] if back else []),
            [button("В админку", "admin_panel")]]


def page_label(result):
    pages = max(1, (result["total"] + result["page_size"] - 1) // result["page_size"])
    return f"Страница {result['page'] + 1}/{pages}"


def overview_keyboard(days):
    return InlineKeyboardMarkup(inline_keyboard=[
        periods(days, lambda d: f"adm_stats_{d}"),
        [button("👥 По людям", f"adm_stats_users_{days}_0"),
         button("🎓 По группам", f"adm_stats_groups_{days}_0")],
        [button("🕒 Последние действия", f"adm_stats_people_{days}_all_0")],
        [button("Обновить", f"adm_stats_{days}"), button("В админку", "admin_panel")],
    ])


def overview(stats, days):
    opens = sum(row["opens"] for row in stats["sections"])
    share = round(stats['active'] / stats['total'] * 100) if stats['total'] else 0
    lines = ["📊 <b>Активность бота</b>", f"{period_label(days)} · время по Минску", "",
             f"👥 Пользовались ботом: <b>{stats['active']} из {stats['total']}</b>",
             f"👆 Открытий разделов: <b>{opens}</b>", f"Охват студентов: <b>{share}%</b>", "",
             "<b>Чаще всего открывают</b>"]
    counts = {row['section']: row['opens'] for row in stats['sections']}
    for section in sorted(SECTIONS, key=lambda key: -counts.get(key, 0)):
        icon, label = SECTIONS[section]
        lines.append(f"{icon} {label}  <b>{counts.get(section, 0)}</b>")
    if not opens:
        lines += ["", "За этот период сохранённых действий пока нет."]
    lines += ["", "<i>Считаются открытия разделов. Действия администратора не учитываются.</i>"]
    return "\n".join(lines), overview_keyboard(days)


def users_screen(result, days):
    lines = ["👥 <b>Кто пользуется ботом</b>", f"{period_label(days)} · активных студентов: <b>{result['total']}</b>", ""]
    rows = [periods(days, lambda d: f"adm_stats_users_{d}_0")]
    for user in result['users']:
        lines += [f"<b>{clean(name_of(user))}</b> · {identity(user)}",
                  f"{clean(user.get('student_group') or 'Без группы', 32)} · открытий: <b>{user['opens']}</b>",
                  f"Последняя активность: {when(user['last_seen'])}", ""]
        label = f"{name_of(user)[:32]} · {user['opens']} ›"
        rows.append([button(label, f"adm_stats_user_{days}_{user['user_id']}_{result['page']}_0")])
    if not result['users']:
        lines += ["Пока никто не открывал разделы за этот период.", ""]
    else:
        lines += ["Нажми на человека, чтобы увидеть его действия.", page_label(result)]
    rows += pagination(result['page'], result['total'], result['page_size'], lambda p: f"adm_stats_users_{days}_{p}")
    rows += footer(days, f"adm_stats_users_{days}_{result['page']}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def events_screen(result, days, section="all", user_id=None, list_page=0):
    personal = user_id is not None
    lines = ["👤 <b>Действия студента</b>" if personal else "🕒 <b>Последние действия</b>",
             f"{period_label(days)} · время по Минску", ""]
    if personal and result['events']:
        user = result['events'][0]
        lines += [f"<b>{clean(name_of(user))}</b> · {identity(user)}", ""]
    elif not personal and section != 'all':
        lines += [f"{SECTIONS[section][0]} {SECTIONS[section][1]}", ""]
    for event in result['events']:
        icon, label = SECTIONS.get(event['section'], ("•", "Раздел"))
        if not personal:
            lines.append(f"<b>{clean(name_of(event), 48)}</b> · {identity(event)}")
        group = clean(event.get('student_group') or 'Без группы', 32)
        lines += [f"{icon} {label} · {group}", when(event['created_at']), ""]
    if not result['events']:
        lines += ["За этот период сохранённых действий нет.", ""]
    else:
        lines += [f"Всего открытий: <b>{result['total']}</b>", page_label(result)]
    if personal:
        target = lambda d, p: f"adm_stats_user_{d}_{user_id}_{list_page}_{p}"
    else:
        target = lambda d, p: f"adm_stats_people_{d}_{section}_{p}"
    rows = [periods(days, lambda d: target(d, 0))]
    if not personal:
        choices = [("Все разделы", "all")] + [(label, key) for key, (_, label) in SECTIONS.items()]
        for start in range(0, len(choices), 2):
            rows.append([button(("✓ " if key == section else "") + label, f"adm_stats_people_{days}_{key}_0")
                         for label, key in choices[start:start + 2]])
    rows += pagination(result['page'], result['total'], result['page_size'], lambda p: target(days, p))
    rows += footer(days, target(days, result['page']), f"adm_stats_users_{days}_{list_page}" if personal else None)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def groups_screen(stats, days, page=0):
    groups = stats['groups']
    page = min(max(0, int(page)), max(0, (len(groups) - 1) // PAGE_SIZE))
    lines = ["🎓 <b>Активность по группам</b>", f"{period_label(days)} · студентов: <b>{stats['active']}</b>", ""]
    for index, group in enumerate(groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE + 1):
        lines.append(f"{index}. <b>{clean(group['group_name'], 40)}</b> · активных: <b>{group['active']}</b>")
    if not groups:
        lines.append("За этот период активности нет.")
    else:
        lines += ["", f"Групп с активностью: {len(groups)}", page_label({'total': len(groups), 'page': page, 'page_size': PAGE_SIZE})]
    lines += ["", "<i>При смене учебной группы студент может попасть в несколько строк.</i>"]
    rows = [periods(days, lambda d: f"adm_stats_groups_{d}_0")]
    rows += pagination(page, len(groups), PAGE_SIZE, lambda p: f"adm_stats_groups_{days}_{p}")
    rows += footer(days, f"adm_stats_groups_{days}_{page}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
