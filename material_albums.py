"""Collect material photos before aiogram selects a state-specific handler."""
import asyncio
from aiogram import BaseMiddleware


def upload_source_key(message):
    group = getattr(message, 'media_group_id', None)
    suffix = f'album:{group}' if group else f'message:{message.message_id}'
    return f'{message.chat.id}:{message.from_user.id}:{suffix}'


def material_bundle(messages):
    ordered = sorted(messages, key=lambda item: item.message_id)
    attachments = []
    for message in ordered:
        caption = (message.caption or '').strip()
        if message.photo:
            attachments.append(dict(file_id=message.photo[-1].file_id, file_type='photo', caption=caption))
        elif message.document:
            attachments.append(dict(file_id=message.document.file_id, file_type='document', caption=caption))
    return dict(source_key=upload_source_key(ordered[0]), attachments=attachments,
                caption=next((item['caption'] for item in attachments if item['caption']), ''))


class MaterialAlbumMiddleware(BaseMiddleware):
    def __init__(self, upload_states, seen, revision, settle_seconds=1.0):
        self.upload_states = set(upload_states)
        self.seen = seen
        self.revision = revision
        self.settle_seconds = settle_seconds
        self.pending = {}
        self.users = {}

    async def __call__(self, handler, event, data):
        state = data.get('state')
        if (not state or event.chat.type != 'private'
                or not (event.photo or event.document)):
            return await handler(event, data)
        current = await state.get_state()
        # Unsolicited photos use the same collector; other features (including
        # schedule albums and feedback photos) keep their original handlers.
        if current not in self.upload_states and not (current is None and event.photo):
            return await handler(event, data)
        source_key = upload_source_key(event)
        if await self.seen(source_key):
            return
        if source_key in self.pending:
            self.pending[source_key][event.message_id] = event
            return
        messages = self.pending[source_key] = {event.message_id: event}
        context = await state.get_data()
        revision = await self.revision(event.from_user.id)
        user_key = (event.chat.id, event.from_user.id)
        entry = self.users.setdefault(user_key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            if getattr(event, 'media_group_id', None):
                while True:
                    count = len(messages)
                    await asyncio.sleep(self.settle_seconds)
                    if len(messages) == count:
                        break
            async with entry[0]:
                live = await state.get_data()
                live_state = await state.get_state()
                same_session = context.get('material_upload_session') == live.get('material_upload_session')
                # Another collected album may have just created the work or
                # finished an older title step. Keep it as a separate upload.
                created_work = (current and current.endswith(':waiting_for_quick_task_title')
                                and live_state and live_state.endswith(':waiting_for_material_file')
                                and live.get('material_upload_origin') == revision
                                and live.get('subject_id') == context.get('subject_id'))
                finished_title = (current and current.endswith(':waiting_for_material_title')
                                  and live_state and live_state.endswith(':waiting_for_material_file')
                                  and same_session)
                if live_state != current and not (created_work or finished_title):
                    return
                if not same_session and not created_work:
                    return
                # Navigation invalidates an unsolicited upload while collecting.
                # Another unsolicited album may legitimately advance the screen.
                if current is None and await self.revision(event.from_user.id) != revision:
                    if live.get('unsolicited_ui_revision') != await self.revision(event.from_user.id):
                        return
                if await self.seen(source_key):
                    return
                data['material_upload'] = dict(material_bundle(list(messages.values())), origin_revision=revision)
                data['raw_state'] = live_state
                return await handler(messages[min(messages)], data)
        finally:
            self.pending.pop(source_key, None)
            entry[1] -= 1
            if not entry[1]:
                self.users.pop(user_key, None)
