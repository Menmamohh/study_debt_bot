"""Telegram screens for explicit dates and individual sheet replacements."""
import asyncio
import datetime as dt
import html
import hashlib
import json
import os
import re
import uuid

from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

import database as db
import dated_schedules as store


class DatedForm(StatesGroup):
    date = State()
    editing = State()


def keyboard(rows):
    return Markup(inline_keyboard=[[Button(text=label, callback_data=value) for label, value in row] for row in rows])


def today():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()


class DatedScheduleUI:
    def __init__(self, app):
        self.app = app
        self.lock = None

    def register(self):
        self.app.dp.callback_query.register(self.callback, F.data.startswith(('ds:', 'dse:', 'dsv:', 'dsl:')))
        self.app.dp.message.register(self.date_input, DatedForm.date)

    async def screen(self, target, text, rows, photos=(), extra_keep=()):
        chat = target.message.chat if hasattr(target, 'message') else target.chat
        keep = list(extra_keep)
        # Each successful photo is tracked before a later send can fail. Old UI is
        # only cleaned once the replacement text screen has succeeded.
        for photo, caption in photos:
            if not photo:
                continue
            source = FSInputFile(photo) if os.path.isfile(photo) else photo
            sent = await self.app.send_tracked_ui(target.bot, chat.id, 'send_photo', photo=source,
                                                  caption=caption[:1000], parse_mode=None)
            keep.append(sent.message_id)
        if len(text) > 4000:
            plain = html.unescape(re.sub(r'</?(?:b|i)>', '', text))
            for offset in range(0, len(plain), 3500):
                sent = await self.app.send_tracked_ui(target.bot, chat.id, 'send_message',
                    text=plain[offset:offset + 3500], parse_mode=None)
                keep.append(sent.message_id)
            text = '📅 Подробности листов показаны выше. Выбери действие:'
        await self.app.show_user_screen(target, text, reply_markup=keyboard(rows), keep_message_ids=keep)

    async def start(self, target, state, group=None, sheet_id=None):
        await state.clear()
        await state.update_data(ds_token=uuid.uuid4().hex[:12], ds_ops=[], ds_index=0,
                                ds_group=group if group in db.COLLEGE_GROUPS else None,
                                ds_replace=sheet_id)
        await state.set_state(self.app.Form.waiting_for_group_schedule)
        await self.screen(target,
            '📅 <b>Подготовка расписаний</b>\n\nОтправь фото, альбом или текст. '
            'Для каждого листа отдельно подтвердим дату и группы. Можно добавить листы на несколько дней. '
            'До общего подтверждения публикации сохраняются.' +
            ('\n\nБудет заменён только выбранный лист. Дата замены сохранится.' if sheet_id else ''),
            [[('Выбрать файлы с Диска', 'ds:drive')], [('Отмена', 'ds:cancel')]])

    async def import_review(self, target, state, draft):
        await state.clear()
        op = {'photo': '', 'body': draft['body'], 'label': 'Проверенный текст замен',
              'received_at': draft['created_at'], 'source_key': f"review:{draft['id']}",
              'review_id': draft['id'], 'suggested_date': draft['schedule_date'],
              'date_confirmed': False, 'groups': [draft['group_name']]}
        # Reuse only the prior text slot of this group and date; source sheets are
        # never guessed from OCR text or silently replaced.
        for sheet in await asyncio.to_thread(store.sheets, draft['schedule_date'], draft['group_name']):
            if sheet['source_key'].startswith('review:') and sheet['scope'] == draft['group_name']:
                op.update(sheet_id=sheet['id'], date=draft['schedule_date'], date_confirmed=False)
                await self.set_expected(op)
                break
        await state.update_data(ds_token=uuid.uuid4().hex[:12], ds_ops=[op], ds_index=0)
        await state.set_state(DatedForm.editing)
        await self.card(target, state, True)

    async def accept_upload(self, message, state, telegram, album=None):
        data = await state.get_data()
        if not data.get('ds_token'):
            # Old entry points are routed into the new dated editor.
            await state.update_data(ds_token=uuid.uuid4().hex[:12], ds_ops=[], ds_index=0,
                                    ds_group=data.get('target_group') if data.get('target_group') in db.COLLEGE_GROUPS else None)
            data = await state.get_data()
        inputs = album or [message]
        if data.get('ds_replace') and len(inputs) != 1:
            await self.screen(message, 'Для замены одного листа отправь одну фотографию или один текст.',
                              [[('Отмена', 'ds:cancel')]])
            return
        ops = list(data.get('ds_ops', []))
        initial = len(ops)
        for i, item in enumerate(inputs):
            photo = item.photo[-1].file_id if item.photo else ''
            body = (item.caption if item.photo else item.text) or ''
            if not photo and not body.strip():
                continue
            op = {'photo': photo, 'body': body, 'label': f'Загруженный лист {len(ops) + 1}',
                  'received_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'suggested_date': store.suggest_date(body), 'date_confirmed': False,
                  'groups': [data['ds_group']] if data.get('ds_group') else []}
            if data.get('ds_replace'):
                with db.get_connection() as conn:
                    old = conn.execute('SELECT * FROM dated_sheets WHERE id=?', (data['ds_replace'],)).fetchone()
                if not old:
                    return
                op.update(sheet_id=old['id'], date=old['schedule_date'], date_confirmed=True)
            ops.append(op)
        if len(ops) == initial:
            await self.screen(message, 'Отправь фото или текст расписания.', [[('Отмена', 'ds:cancel')]])
            return
        await state.update_data(ds_ops=ops, ds_index=initial, ds_replace=None)
        await state.set_state(DatedForm.editing)
        await self.card(message, state, show_photos=True)

    async def card(self, target, state, show_photos=False):
        data = await state.get_data()
        ops, index, token = data['ds_ops'], data['ds_index'], data['ds_token']
        op = ops[index]
        def action(name, arg=''):
            return f'dse:{token}:{name}:{arg}'
        day = op.get('date') if op.get('date_confirmed') else 'не подтверждена'
        groups = ', '.join(op.get('groups', [])) or 'не выбраны — укажи область изменения'
        hint = op.get('suggested_date') or 'не найдена'
        text = (f'📄 <b>Лист {index + 1} из {len(ops)}</b> — {html.escape(op["label"][:100])}\n'
                f'Получен (UTC): {html.escape(op["received_at"])}\n'
                f'Предложенная дата из исходника: {hint}\n'
                f'<b>Дата действия: {day}</b>\nГруппы: {html.escape(groups)}\n'
                f'Действие: {"заменить лист №" + str(op["sheet_id"]) if op.get("sheet_id") else "добавить лист"}\n\n'
                + html.escape(op.get('body', '')))
        rows = []
        if op.get('sheet_id') and not op.get('date_confirmed'):
            rows.append([(f'Подтвердить дату {op["date"]}', action('confirm'))])
        if not op.get('sheet_id'):
            if op.get('suggested_date'):
                rows.append([(f'Подтвердить {op["suggested_date"]}', action('day', op['suggested_date']))])
            rows.append([('Сегодня', action('day', today().isoformat())),
                         ('Завтра', action('day', (today() + dt.timedelta(days=1)).isoformat()))])
            rows.append([('Ввести дату', action('date'))])
        rows.append([('Все группы' + (' ✓' if op.get('groups') == ['ALL'] else ''), action('all'))])
        # Explicit group selection is required even when OCR cannot determine scope.
        for offset in range(0, len(db.COLLEGE_GROUPS), 3):
            rows.append([(('✓ ' if group in op.get('groups', []) else '') + group, action('group', group))
                         for group in db.COLLEGE_GROUPS[offset:offset + 3]])
        if op.get('date_confirmed') and not op.get('sheet_id'):
            rows.append([('Вместо существующего листа…', action('targets'))])
        rows.append([('◀ Предыдущий лист', action('prev')), ('Следующий лист ▶', action('next'))])
        rows.append([('Показать сравнение', action('compare')), ('Общий предпросмотр', action('preview'))])
        rows.append([('Добавить листы', action('add')), ('Отмена', 'ds:cancel')])
        photos = []
        if show_photos:
            if op.get('sheet_id'):
                for old in await asyncio.to_thread(self.old_variants, op):
                    photos.append((old['photo'], 'БЫЛО: ' + old['scope'] + ', ' + op['date']))
                    if old['body']:
                        text += '\n\n<b>Было (' + html.escape(old['scope']) + '):</b>\n' + html.escape(old['body'])
            photos.append((op.get('photo'), 'БУДЕТ: ' + str(day) + ', ' + groups))
        await self.screen(target, text, rows, photos)

    def old_variants(self, op):
        groups = list(db.COLLEGE_GROUPS) + ['ALL'] if op.get('groups') == ['ALL'] else op.get('groups', [])
        found = {}
        for group in groups:
            for row in store.sheets(op['date'], group):
                if row['id'] == op['sheet_id']:
                    found[row['revision_id']] = row
        return list(found.values())

    async def set_expected(self, op):
        if op.get('sheet_id'):
            groups = list(db.COLLEGE_GROUPS) + ['ALL'] if op.get('groups') == ['ALL'] else op.get('groups', [])
            op['expected'] = await asyncio.to_thread(store.expected_heads, op['sheet_id'], groups)

    async def preview(self, target, state):
        data = await state.get_data()
        ops, token = data['ds_ops'], data['ds_token']
        invalid = [i for i, op in enumerate(ops) if not op.get('date_confirmed') or not op.get('groups')]
        if invalid:
            await state.update_data(ds_index=invalid[0])
            await self.card(target, state)
            return
        photos = []
        lines = ['📅 <b>Общий предпросмотр</b>', 'Каждая дата сохранится отдельно.']
        for day in sorted({op['date'] for op in ops}):
            lines.append('\n<b>' + day + '</b>')
            for i, op in enumerate(ops):
                if op['date'] != day:
                    continue
                groups = op['groups']
                users = await self.app.schedule_publication_recipients(
                    [] if groups == ['ALL'] else groups, groups == ['ALL'], target.from_user.id)
                verb = 'Замена №' + str(op['sheet_id']) if op.get('sheet_id') else 'Добавление'
                lines.append(f'• Лист {i + 1}: {verb}; {html.escape(", ".join(groups))}; получателей: {len(users)}')
                if op.get('sheet_id'):
                    for old in await asyncio.to_thread(self.old_variants, op):
                        photos.append((old['photo'], f'БЫЛО: лист №{op["sheet_id"]}, {day}, {old["scope"]}'))
                        if old['body']:
                            lines.append('Было: ' + html.escape(old['body']))
                    photos.append((op.get('photo'), f'БУДЕТ: лист №{op["sheet_id"]}, {day}, {", ".join(groups)}'))
                    if op.get('body'):
                        lines.append('Будет: ' + html.escape(op['body']))
        lines.append('\nНеуказанные листы, группы и даты сохраняются. Уведомление содержит ссылку на дату; остальные фото повторно не отправляются.')
        rows = [[(f'Проверить лист {i + 1}', f'dse:{token}:edit:{i}')] for i in range(len(ops))]
        await state.update_data(ds_preview_signature=signature(ops))
        rows += [[('Опубликовать все даты', f'dse:{token}:publish:')],
                 [('Добавить листы', f'dse:{token}:add:'), ('Отмена', 'ds:cancel')]]
        await self.screen(target, '\n'.join(lines), rows, photos)

    async def date_input(self, message, state):
        if not self.app.is_admin(message.from_user) or message.chat.type != 'private':
            return
        data = await state.get_data()
        if not data.get('ds_token'):
            return
        text = (message.text or '').strip()
        day = store.suggest_date(text)
        # Accept a full explicit date only; no surrounding prose and no implicit year here.
        try:
            if re_full_iso(text):
                day = store.valid_date(text)
            else:
                day = dt.datetime.strptime(text, '%d.%m.%Y').date().isoformat()
        except ValueError:
            await self.screen(message, 'Введи дату целиком: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.', [[('Отмена', 'ds:cancel')]])
            return
        ops = data['ds_ops']
        ops[data['ds_index']].update(date=day, date_confirmed=True)
        await state.update_data(ds_ops=ops)
        await state.set_state(DatedForm.editing)
        await self.card(message, state)

    async def render(self, target, user, group, day=None, batch=None):
        available = await asyncio.to_thread(store.dates, group)
        if not available:
            legacy = await asyncio.to_thread(db.get_schedule_versions, group)
            approved = await asyncio.to_thread(
                self.app.schedule_review.approved_schedule if day else self.app.schedule_review.next_approved_schedule,
                group, dt.date.fromisoformat(day) if day else today())
            if legacy or approved:
                return False
        day = day or next((d for d in sorted(available) if d >= today().isoformat()),
                          available[0] if available else today().isoformat())
        rows_data = await asyncio.to_thread(store.sheets, day, group, batch)
        history = await asyncio.to_thread(store.publications, day, group)
        rows = []
        if batch:
            rows.append([('Актуальная версия этого дня', f'dsv:{day}:{group}')])
        if history:
            older = next((h['id'] for h in history if h['id'] < (batch or history[0]['id'])), None)
            if older:
                rows.append([('Предыдущая версия этого дня', f'dsv:{day}:{group}:{older}')])
        # Bound the keyboard while permitting access to every date via pagination.
        page = available.index(day) // 8 if day in available else 0
        for d in available[page * 8:page * 8 + 8]:
            rows.append([(('✓ ' if d == day else '') + d, f'dsv:{d}:{group}')])
        if page:
            rows.append([('Более новые даты', f'dsv:{available[(page - 1) * 8]}:{group}')])
        if len(available) > (page + 1) * 8:
            rows.append([('Более старые даты', f'dsv:{available[(page + 1) * 8]}:{group}')])
        if self.app.is_admin(user) and (target.message.chat if hasattr(target, 'message') else target.chat).type == 'private':
            rows.append([('Управлять листами', 'ds:manage'), ('Добавить расписание', 'ds:start')])
        rows += [[('Архив прежних публикаций', f'dsl:{group}')],
                 [('Выбрать группу', 'sched_choose_group'), ('Расписание звонков', 'sched_bells')],
                 [('Главное меню', 'main_menu')]]
        label = 'Сохранённая версия' if batch else 'Расписание'
        text = f'📅 <b>{label} — {html.escape(group)}, {day}</b>\n'
        if not rows_data:
            text += '\nНа эту дату для группы нет опубликованных листов.'
        photos = []
        for i, sheet in enumerate(rows_data):
            text += f'\n<b>Лист {i + 1}</b> (№{sheet["id"]})\n' + html.escape(sheet['body']) + '\n'
            photos.append((sheet['photo'], f'{day} · {group} · лист {i + 1} (№{sheet["id"]})'))
        await self.screen(target, text, rows, photos)
        return True

    async def callback(self, callback, state):
        raw = callback.data.split(':')
        if raw[0] in ('dsv', 'dsl'):
            ok, error = await self.app.check_user_access(callback.from_user)
            if not ok:
                await self.app.prompt_verification_required(callback, callback.from_user, error)
                return
            await state.clear()
            try:
                if raw[0] == 'dsl':
                    group = raw[1]
                    if group not in {*db.COLLEGE_GROUPS, 'ALL'}:
                        raise ValueError
                    await self.app.render_group_schedule_view(callback, callback.from_user, group,
                                                              is_callback=True, legacy_only=True, notice='Архив прежних публикаций. Дата действия могла быть не указана.')
                    return
                day, group = store.valid_date(raw[1]), raw[2]
                if group == 'ALL' and not self.app.is_admin(callback.from_user):
                    group = await self.app.get_user_primary_group(callback.from_user.id) or group
                if group not in {*db.COLLEGE_GROUPS, 'ALL'}:
                    raise ValueError
                batch = int(raw[3]) if len(raw) == 4 else None
                await self.render(callback, callback.from_user, group, day, batch)
            except (ValueError, IndexError):
                await callback.answer('Открой расписание заново.', show_alert=True)
            return
        if not self.app.schedule_publication_admin(callback):
            await callback.answer('Только администратору в личном чате.', show_alert=True)
            return
        await callback.answer()
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            try:
                await self.admin_callback(callback, state, raw)
            except (ValueError, KeyError, IndexError) as error:
                await self.screen(callback, '⚠️ ' + html.escape(str(error)) + '\nПодготовь операцию заново.',
                                  [[('Управление листами', 'ds:manage'), ('Отмена', 'ds:cancel')]])

    async def admin_callback(self, cb, state, parts):
        if parts[0] == 'ds':
            action = parts[1]
            if action == 'cancel':
                await state.clear()
                await self.screen(cb, 'Подготовка отменена. Опубликованные расписания сохранены.',
                                  [[('Расписание', 'schedule_menu'), ('Главное меню', 'main_menu')]])
            elif action == 'start':
                await self.start(cb, state)
            elif action == 'manage':
                with db.get_connection() as conn:
                    days = [r[0] for r in conn.execute('SELECT DISTINCT schedule_date FROM dated_sheets ORDER BY schedule_date DESC')]
                page = int(parts[2]) if len(parts) > 2 else 0
                rows = [[(d, f'ds:day:{d}')] for d in days[page*12:page*12+12]]
                if page:
                    rows.append([('Более новые', f'ds:manage:{page-1}')])
                if len(days) > (page+1)*12:
                    rows.append([('Более старые', f'ds:manage:{page+1}')])
                rows += [[('Добавить расписание', 'ds:start'), ('Главное меню', 'main_menu')]]
                await state.clear()
                await self.screen(cb, '📅 Выбери дату для замены отдельного листа.\nСтарые публикации без даты доступны в архиве.', rows)
            elif action == 'day':
                day = store.valid_date(parts[2])
                with db.get_connection() as conn:
                    items = conn.execute('SELECT * FROM dated_sheets WHERE schedule_date=? ORDER BY position,id', (day,)).fetchall()
                rows = [[(f'Лист {i+1} · №{r["id"]} · {r["label"][:30]}', f'ds:replace:{r["id"]}')] for i,r in enumerate(items)]
                rows += [[('Назад к датам', 'ds:manage'), ('Главное меню', 'main_menu')]]
                await self.screen(cb, f'📅 {day}\nВыбери лист. Перед публикацией покажу старую и новую версии.', rows)
            elif action == 'replace':
                await self.start(cb, state, sheet_id=int(parts[2]))
            elif action == 'drive':
                data = await state.get_data()
                if not data.get('ds_token'):
                    await self.start(cb, state)
                candidates = await asyncio.to_thread(store.drive_candidates)
                page = int(parts[2]) if len(parts) > 2 else 0
                pick_token = uuid.uuid4().hex[:12]
                await state.update_data(ds_candidates=candidates, ds_candidates_token=pick_token)
                rows = [[(('✓ ' if c['published'] else '') + c['label'][:45], f'ds:pick:{pick_token}:{i}')
                         ] for i,c in enumerate(candidates) if page*12 <= i < page*12+12]
                if page:
                    rows.append([('Предыдущие файлы', f'ds:drive:{page-1}')])
                if len(candidates) > (page+1)*12:
                    rows.append([('Следующие файлы', f'ds:drive:{page+1}')])
                rows.append([('Отмена', 'ds:cancel')])
                await self.screen(cb, 'Файлы с Диска — исходники, дата пока предложенная. ✓ — содержимое уже публиковалось.', rows)
            elif action == 'pick':
                data = await state.get_data()
                if len(parts) != 4 or parts[2] != data.get('ds_candidates_token'):
                    raise ValueError('Этот список исходников устарел.')
                op = dict(data['ds_candidates'][int(parts[3])])
                op.update(groups=[],date_confirmed=False)
                if data.get('ds_replace'):
                    with db.get_connection() as conn:
                        row = conn.execute('SELECT * FROM dated_sheets WHERE id=?', (data['ds_replace'],)).fetchone()
                    op.update(sheet_id=row['id'],date=row['schedule_date'],date_confirmed=True)
                ops = data['ds_ops'] + [op]
                await state.update_data(ds_ops=ops, ds_index=len(ops)-1, ds_replace=None)
                await state.set_state(DatedForm.editing)
                await self.card(cb, state, True)
            return
        data = await state.get_data()
        if len(parts) != 4 or parts[1] != data.get('ds_token'):
            raise ValueError('Кнопка завершённой или отменённой операции.')
        action, arg = parts[2:]
        ops, index = data['ds_ops'], data['ds_index']
        if not ops:
            raise ValueError('Сначала добавь листы.')
        op = ops[index]
        if action == 'publish':
            if data.get('ds_preview_signature') != signature(ops):
                await self.preview(cb, state)
                return
            result = await asyncio.to_thread(store.publish, data['ds_token'], ops, str(cb.from_user.id))
            await state.clear()
            sent, failed = await self.app.deliver_schedule_notifications(cb.bot)
            await self.screen(cb, ('✅ Опубликовано: ' + ', '.join(result['dates']) if result['changed'] else 'Уже опубликовано.') +
                              f'\nУведомлений отправлено: {sent}, ожидают повтора: {failed}.',
                              [[('Открыть расписание', 'schedule_menu'), ('Главное меню', 'main_menu')]])
            return
        if action == 'preview':
            if not ops:
                raise ValueError('Сначала добавь листы.')
            await self.preview(cb, state)
            return
        if action == 'add':
            await state.set_state(self.app.Form.waiting_for_group_schedule)
            await self.screen(cb, 'Отправь следующие листы или выбери файлы с Диска.',
                              [[('Выбрать с Диска', 'ds:drive'), ('Предпросмотр', f'dse:{data["ds_token"]}:preview:')], [('Отмена','ds:cancel')]])
            return
        if action == 'date':
            await state.set_state(DatedForm.date)
            await self.screen(cb, 'Введи дату действия листа: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.', [[('Отмена','ds:cancel')]])
            return
        if action == 'targets':
            if not op.get('groups'):
                raise ValueError('Сначала выбери группы для изменения.')
            found = {}
            groups = list(db.COLLEGE_GROUPS) + ['ALL'] if op['groups'] == ['ALL'] else op['groups']
            for group in groups:
                for item in await asyncio.to_thread(store.sheets, op['date'], group):
                    found[item['id']] = item
            rows = [[(f'Лист №{r["id"]} · {r["label"][:35]}', f'dse:{data["ds_token"]}:target:{r["id"]}')] for r in found.values()]
            rows.append([('Назад', f'dse:{data["ds_token"]}:edit:{index}')])
            await self.screen(cb, 'Выбери лист, который заменит загруженный.', rows)
            return
        if action == 'confirm':
            op['date_confirmed'] = True
        elif action == 'target':
            sid = int(arg)
            with db.get_connection() as conn:
                row = conn.execute('SELECT * FROM dated_sheets WHERE id=?', (sid,)).fetchone()
            if not row or row['schedule_date'] != op['date']:
                raise ValueError('Лист относится к другой дате.')
            op['sheet_id'] = sid
            await self.set_expected(op)
        elif action == 'day':
            if op.get('sheet_id'):
                raise ValueError('Замена сохраняет дату исходного листа.')
            op.update(date=store.valid_date(arg),date_confirmed=True)
        elif action == 'all':
            op['groups'] = [] if op.get('groups') == ['ALL'] else ['ALL']
            await self.set_expected(op)
        elif action == 'group' and arg in db.COLLEGE_GROUPS:
            groups = [] if op.get('groups') == ['ALL'] else list(op.get('groups', []))
            groups.remove(arg) if arg in groups else groups.append(arg)
            op['groups'] = groups
            await self.set_expected(op)
        elif action in ('prev','next','edit'):
            index = int(arg) if action == 'edit' else (index + (1 if action == 'next' else -1)) % len(ops)
        await state.update_data(ds_ops=ops,ds_index=index)
        await state.set_state(DatedForm.editing)
        await self.card(cb, state, show_photos=action in ('compare','target','edit','prev','next'))


def re_full_iso(text):
    import re
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', text))


def signature(ops):
    return hashlib.sha256(json.dumps(ops, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
