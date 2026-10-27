"""Material uploads: temporary SQLite, real FSM and mocked Telegram; no network."""
import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, PhotoSize, Update, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot
import database as db
import keyboards as kb
from material_albums import MaterialAlbumMiddleware, material_bundle


class MaterialAlbumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'materials.db')
        db.init_db()
        self.storage = MemoryStorage()
        self.user = NS(id=10, username='student', full_name='Student')
        db.add_user_request(10, 'student', 'Student', 'Student', 'ТТ-36')
        db.set_user_status(10, 'approved')
        self.subject = db.add_subject(10, 'Материаловедение')
        self.task = db.add_task(10, self.subject, 'Работа')
        self.state = self.make_state(10)
        self.sent_id = 1000
        self.sent_groups = []
        self.telegram = NS(send_message=AsyncMock(side_effect=self.sent),
                           send_photo=AsyncMock(side_effect=self.sent),
                           send_document=AsyncMock(side_effect=self.sent),
                           send_media_group=AsyncMock(side_effect=self.album_sent),
                           delete_message=AsyncMock())
        self.admin_patch = patch.object(bot, 'ADMIN_ID', 99)
        self.admin_patch.start()
        self.middleware = MaterialAlbumMiddleware(
            {bot.Form.waiting_for_material_file.state, bot.Form.waiting_for_material_title.state,
             bot.Form.waiting_for_quick_task_title.state},
            bot.adb.material_upload_seen, bot.adb.get_ui_revision, settle_seconds=0.015)

    def tearDown(self):
        self.admin_patch.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def make_state(self, user_id):
        return FSMContext(self.storage, StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    async def sent(self, *args, **kwargs):
        self.sent_id += 1
        return NS(message_id=self.sent_id)

    async def album_sent(self, **kwargs):
        result = [await self.sent() for _ in kwargs['media']]
        self.sent_groups.append(result)
        return result

    def message(self, number=1, group=None, caption=None, user=None, document=False, text=None):
        user = user or self.user
        return NS(message_id=number, media_group_id=group, caption=caption, text=text,
                  photo=[NS(file_id=f'photo-{user.id}-{number}')] if not document and text is None else None,
                  document=NS(file_id='document-id', file_name='guide.pdf') if document else None,
                  from_user=user, chat=NS(id=user.id, type='private'), bot=self.telegram,
                  answer=AsyncMock(side_effect=self.telegram.send_message),
                  answer_photo=AsyncMock(side_effect=self.telegram.send_photo))

    def callback(self, data, user=None, message_id=500):
        user = user or self.user
        return NS(data=data, from_user=user, bot=self.telegram, answer=AsyncMock(),
                  message=NS(chat=NS(id=user.id, type='private'), message_id=message_id,
                             caption=None, photo=None, document=None, text='Card',
                             edit_text=AsyncMock(), edit_caption=AsyncMock(), bot=self.telegram,
                             answer=AsyncMock(side_effect=self.telegram.send_message),
                             answer_photo=AsyncMock(side_effect=self.telegram.send_photo),
                             answer_document=AsyncMock(side_effect=self.telegram.send_document)))

    async def start_upload(self, state=None, task=None, subject=None):
        state = state or self.state
        await state.set_data(dict(mat_type='method', task_id=task or self.task,
                                  subject_id=subject or self.subject, material_upload_session='session'))
        await state.set_state(bot.Form.waiting_for_material_file)

    async def deliver(self, message, state=None):
        state = state or self.state
        async def dispatch(event, data):
            current = data['raw_state']
            if current == bot.Form.waiting_for_quick_task_title.state:
                await bot.process_quick_task_title(event, state, self.telegram, data['material_upload'])
            elif current in {bot.Form.waiting_for_material_file.state, bot.Form.waiting_for_material_title.state}:
                await bot.process_material_file(event, state, self.telegram, data['material_upload'])
            elif current is None:
                await bot.cb_photo_unsolicited(event, state, data['material_upload'])
        await self.middleware(dispatch, message, {'state': state})

    def materials(self):
        with db.get_connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM materials ORDER BY id')]

    async def two_photos(self, group='album', caption='Глава 2'):
        # Arrival order differs from Telegram's original message order.
        await asyncio.gather(self.deliver(self.message(2, group, caption)),
                             self.deliver(self.message(1, group)))
        return self.materials()[-1]

    async def test_single_photo_and_document_still_work(self):
        await self.start_upload()
        await self.deliver(self.message(caption='Одна страница'))
        await self.deliver(self.message(2, document=True))
        rows = self.materials()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['file_type'] for row in rows], ['photo', 'document'])
        self.assertTrue(all(row['material_type'] == 'method' for row in rows))
        self.telegram.send_photo.assert_awaited_once()
        self.telegram.send_document.assert_awaited_once()

    async def test_album_order_caption_on_second_one_request_one_card(self):
        await self.start_upload()
        row = await self.two_photos()
        self.assertEqual(len(self.materials()), 1)
        self.assertEqual(row['title'], 'Глава 2')
        files = db.get_material_attachments(row['id'])
        self.assertEqual([f['file_id'] for f in files], ['photo-10-1', 'photo-10-2'])
        self.assertEqual([f['caption'] for f in files], ['', 'Глава 2'])
        self.telegram.send_media_group.assert_awaited_once()
        admin_cards = [c for c in self.telegram.send_message.await_args_list if c.kwargs.get('chat_id') == 99]
        self.assertEqual(len(admin_cards), 1)
        callbacks = [b.callback_data for line in admin_cards[0].kwargs['reply_markup'].inline_keyboard for b in line]
        self.assertEqual(callbacks, [f"adm_appr_{row['id']}", f"adm_rejc_{row['id']}"])

    async def test_different_albums_and_standalone_uploads_stay_separate(self):
        await self.start_upload()
        await asyncio.gather(self.deliver(self.message(1, 'A')), self.deliver(self.message(2, 'A')),
                             self.deliver(self.message(3, 'B')), self.deliver(self.message(4, 'B')))
        await self.deliver(self.message(5, 'C'))
        await self.deliver(self.message(6))
        await self.deliver(self.message(7))
        self.assertEqual(len(self.materials()), 5)
        files = [db.get_material_attachments(row['id']) for row in self.materials()]
        self.assertEqual(sorted(len(f) for f in files), [1, 1, 1, 2, 2])
        self.assertEqual({tuple(f['file_id'] for f in group) for group in files},
                         {('photo-10-1','photo-10-2'), ('photo-10-3','photo-10-4'),
                          ('photo-10-5',), ('photo-10-6',), ('photo-10-7',)})

    async def test_concurrent_users_same_album_id_are_isolated(self):
        await self.start_upload()
        user = NS(id=20, username='other', full_name='Other')
        db.add_user_request(20, 'other', 'Other', 'Other', 'ТТ-36')
        db.set_user_status(20, 'approved')
        subject = db.add_subject(20, 'Предмет')
        task = db.add_task(20, subject, 'Работа')
        other = self.make_state(20)
        await self.start_upload(other, task, subject)
        await asyncio.gather(self.deliver(self.message(1,'same')), self.deliver(self.message(2,'same')),
                             self.deliver(self.message(1,'same',user=user),other),
                             self.deliver(self.message(2,'same',user=user),other))
        self.assertEqual(len(self.materials()), 2)
        for row in self.materials():
            self.assertTrue(all(f"-{row['user_id']}-" in f['file_id'] for f in db.get_material_attachments(row['id'])))

    async def test_repeated_events_and_collector_restart_do_not_duplicate(self):
        await self.start_upload()
        await asyncio.gather(self.deliver(self.message(1,'A')), self.deliver(self.message(2,'A')),
                             self.deliver(self.message(1,'A')))
        db.init_db()
        self.middleware.pending.clear()
        await self.deliver(self.message(1,'A'))
        await self.deliver(self.message(2,'A'))
        await self.deliver(self.message(3))
        await self.deliver(self.message(3))
        self.assertEqual(len(self.materials()), 2)
        self.assertEqual(len(db.get_material_attachments(self.materials()[0]['id'])), 2)
        self.telegram.send_media_group.assert_awaited_once()

    async def test_approval_entire_album_download_cleanup_and_duplicate_decision(self):
        await self.start_upload()
        row = await self.two_photos()
        original_ids = [m['message_id'] for m in db.get_material_moderation_messages(row['id'])]
        admin = NS(id=99)
        cb = self.callback(f"adm_appr_{row['id']}", admin, original_ids[-1])
        with patch.object(bot, 'is_admin', return_value=True):
            await bot.cb_admin_approve(cb, self.telegram)
            await bot.cb_admin_reject(self.callback(f"adm_rejc_{row['id']}", admin), self.telegram)
            await bot.cb_admin_approve(cb, self.telegram)
        self.assertEqual(db.get_material_by_id(row['id'])['status'], 'approved')
        self.assertTrue(set(original_ids).issubset(db.get_user_ui_message_ids(99)))
        author_notices = [c for c in self.telegram.send_message.await_args_list if c.kwargs.get('chat_id') == 10]
        self.assertEqual(len(author_notices), 1)
        self.telegram.delete_message.assert_not_awaited()
        await bot.cb_download_material(self.callback(f"dl_mat_{row['id']}"))
        self.assertEqual(self.telegram.send_media_group.await_count, 2)
        download = self.sent_groups[-1]
        self.assertTrue({m.message_id for m in download}.issubset(db.get_user_ui_message_ids(10)))
        # The album stays visible with its card, then goes away after successful navigation.
        removed_before = {c.kwargs['message_id'] for c in self.telegram.delete_message.await_args_list}
        self.assertFalse(removed_before.intersection(m.message_id for m in download))
        await bot.show_user_screen(self.message(900, text='меню'), 'Главное меню')
        removed = {c.kwargs['message_id'] for c in self.telegram.delete_message.await_args_list}
        self.assertTrue({m.message_id for m in download}.issubset(removed))
        await bot.cleanup_user_ui_messages(self.telegram, 99)
        removed = {c.kwargs['message_id'] for c in self.telegram.delete_message.await_args_list}
        self.assertTrue(set(original_ids).issubset(removed))
        self.assertEqual(len(db.get_material_attachments(row['id'])), 2)

    async def test_rejection_hides_entire_album(self):
        await self.start_upload()
        row = await self.two_photos()
        with patch.object(bot, 'is_admin', return_value=True):
            await bot.cb_admin_reject(self.callback(f"adm_rejc_{row['id']}", NS(id=99)), self.telegram)
        self.assertEqual(db.get_material_by_id(row['id'])['status'], 'rejected')
        self.assertEqual(db.get_approved_materials(self.task), [])
        self.assertEqual(len(db.get_material_attachments(row['id'])), 2)
        count = self.telegram.send_media_group.await_count
        await bot.cb_download_material(self.callback(f"dl_mat_{row['id']}"))
        self.assertEqual(self.telegram.send_media_group.await_count, count)

    async def test_pending_notification_survives_navigation_but_preview_is_temporary(self):
        await self.start_upload()
        row = await self.two_photos()
        ids = {m['message_id'] for m in db.get_material_moderation_messages(row['id'])}
        await bot.cleanup_user_ui_messages(self.telegram, 99, extra_ids=ids)
        removed = {c.kwargs['message_id'] for c in self.telegram.delete_message.await_args_list}
        self.assertFalse(ids.intersection(removed))
        with patch.object(bot, 'is_admin', return_value=True):
            await bot.cb_admin_view_material(self.callback(f"adm_material_view_{row['id']}", NS(id=99)), self.telegram)
        preview_ids = {m['message_id'] for m in db.get_material_moderation_messages(row['id']) if m['kind']=='preview'}
        await bot.cleanup_user_ui_messages(self.telegram, 99)
        removed = {c.kwargs['message_id'] for c in self.telegram.delete_message.await_args_list}
        self.assertTrue(preview_ids.issubset(removed))
        self.assertFalse(ids.intersection(removed))
        self.assertEqual(db.count_pending_materials(), 1)

    async def test_failed_notification_remains_in_queue_and_tracks_partial_album(self):
        await self.start_upload()
        self.telegram.send_message.side_effect = self.sent
        original = bot.send_material_review
        async def broken_notification(*args, **kwargs):
            async def fail_admin(*a, **kw):
                if kw.get('chat_id') == 99:
                    raise RuntimeError('card failed')
                return await self.sent()
            self.telegram.send_message.side_effect = fail_admin
            return await original(*args, **kwargs)
        with patch.object(bot, 'send_material_review', side_effect=broken_notification):
            row = await self.two_photos()
        self.assertEqual(db.count_pending_materials(), 1)
        self.assertEqual(len(db.get_material_moderation_messages(row['id'])), 2)
        self.telegram.send_message.side_effect = self.sent
        with patch.object(bot, 'is_admin', return_value=True):
            await bot.cb_admin_view_material(self.callback(f"adm_material_view_{row['id']}", NS(id=99)), self.telegram)
        self.assertEqual(len(self.materials()), 1)

    async def test_failed_download_card_keeps_previous_screen_and_tracks_photos(self):
        await self.start_upload()
        row = await self.two_photos()
        db.set_material_status(row['id'], 'approved')
        db.set_ui_message_id(10, 700)
        self.telegram.send_message.side_effect = RuntimeError('card unavailable')
        with self.assertRaises(RuntimeError):
            await bot.cb_download_material(self.callback(f"dl_mat_{row['id']}"))
        self.assertEqual(db.get_ui_message_id(10), 700)
        self.assertTrue({m.message_id for m in self.sent_groups[-1]}.issubset(db.get_user_ui_message_ids(10)))
        self.telegram.send_message.side_effect = self.sent
        await bot.show_user_screen(self.message(901, text='меню'), 'Главное меню')
        self.assertEqual(len(db.get_material_attachments(row['id'])), 2)

    async def test_navigation_during_collection_cancels_upload(self):
        await self.start_upload()
        pending = asyncio.create_task(self.deliver(self.message(1,'A')))
        await asyncio.sleep(0.005)
        await self.state.clear()
        await pending
        self.assertEqual(self.materials(), [])
        self.assertFalse(self.middleware.pending)

    async def test_new_work_photo_album_submits_without_type_choice(self):
        await self.state.set_state(bot.Form.waiting_for_quick_task_title)
        await self.state.update_data(subject_id=self.subject)
        row = await self.two_photos(caption='Новая работа')
        self.assertEqual(db.get_task_info(row['task_id'])['title'], 'Новая работа')
        self.assertEqual(len(db.get_material_attachments(row['id'])), 2)
        self.assertEqual(row['material_type'], 'method')

    async def test_new_work_document_keeps_document_type(self):
        await self.state.set_state(bot.Form.waiting_for_quick_task_title)
        await self.state.update_data(subject_id=self.subject)
        await self.deliver(self.message(1, document=True))
        self.assertEqual(self.materials()[0]['file_type'], 'document')
        self.telegram.send_document.assert_awaited_once()
        self.telegram.send_photo.assert_not_awaited()

    async def test_two_albums_during_new_work_creation_are_two_materials(self):
        await self.state.set_state(bot.Form.waiting_for_quick_task_title)
        await self.state.update_data(subject_id=self.subject)
        await asyncio.gather(self.deliver(self.message(1,'A')), self.deliver(self.message(2,'A','Первая')),
                             self.deliver(self.message(3,'B')), self.deliver(self.message(4,'B','Вторая')))
        self.assertEqual(len(self.materials()),2)
        self.assertEqual(len({row['task_id'] for row in self.materials()}),1)
        self.assertTrue(all(len(db.get_material_attachments(row['id']))==2 for row in self.materials()))

    async def test_unsolicited_albums_queue_separately_until_work_selected(self):
        await asyncio.gather(self.deliver(self.message(1,'A')), self.deliver(self.message(2,'A', 'Первый')),
                             self.deliver(self.message(3,'B')), self.deliver(self.message(4,'B', 'Второй')))
        await self.deliver(self.message(1,'A'))  # duplicate before submission
        self.assertEqual(len((await self.state.get_data())['unsolicited_uploads']), 2)
        await bot.cb_quick_task_select(self.callback(f'quick_task_{self.task}_{self.subject}'), self.state)
        self.assertEqual(len(self.materials()), 2)
        self.assertEqual({r['title'] for r in self.materials()}, {'Первый','Второй'})
        self.assertTrue(all(len(db.get_material_attachments(r['id']))==2 for r in self.materials()))

    async def test_all_entrypoints_start_methods_and_legacy_solution_is_blocked(self):
        for handler, prefix in [(bot.cb_quick_task_select,'quick_task'), (bot.cb_upload_material,'upload_mat')]:
            await self.state.clear()
            await handler(self.callback(f'{prefix}_{self.task}_{self.subject}'), self.state)
            self.assertEqual((await self.state.get_data())['mat_type'], 'method')
            self.assertEqual(await self.state.get_state(), bot.Form.waiting_for_material_file.state)
        old = self.callback(f'set_mat_type_solution_{self.task}_{self.subject}')
        await bot.cb_set_material_type(old, self.state, self.telegram)
        self.assertIn('недоступно', old.answer.call_args.args[0])
        # A saved legacy solution session must not fall through to a method upload.
        await self.state.update_data(mat_type='solution', mat_title='Решение')
        await bot.cb_save_text_mat(self.callback(f'save_text_mat_{self.task}_{self.subject}'), self.state, self.telegram)
        await bot.process_material_file(self.message(1), self.state, self.telegram)
        self.assertEqual(self.materials(), [])
        self.assertIsNone(await self.state.get_state())
        markup = kb.choose_material_type_kb(self.task, self.subject)
        self.assertFalse(any('solution' in (b.callback_data or '') for line in markup.inline_keyboard for b in line))

    async def test_text_method_and_repeated_text_button(self):
        await self.start_upload()
        await bot.process_material_file(self.message(20,text='Текст методички'), self.state, self.telegram)
        cb = self.callback(f'save_text_mat_{self.task}_{self.subject}')
        await bot.cb_save_text_mat(cb,self.state,self.telegram)
        await bot.cb_save_text_mat(cb,self.state,self.telegram)
        self.assertEqual(len(self.materials()),1)
        self.assertEqual(self.materials()[0]['content_text'],'Текст методички')

    async def test_old_photo_document_solution_and_text_download_unchanged(self):
        for index, (kind, file_type) in enumerate([('method','photo'),('solution','document'),('solution','text')]):
            mid = db.add_material(10,'student',self.task,kind,'Старый материал',
                                  'old-file' if file_type!='text' else None,file_type,'Старый текст')
            db.set_material_status(mid,'approved')
            await bot.cb_download_material(self.callback(f'dl_mat_{mid}', message_id=500+index))
            self.assertEqual(db.get_material_by_id(mid)['material_type'],kind)
        self.telegram.send_media_group.assert_not_awaited()
        self.telegram.send_photo.assert_awaited_once()
        self.telegram.send_document.assert_awaited_once()

    async def test_other_states_are_not_collected(self):
        await self.state.set_state(bot.Form.waiting_for_student_feedback_reply)
        handler = AsyncMock()
        await self.middleware(handler,self.message(1,'A'),{'state':self.state})
        handler.assert_awaited_once()
        self.assertNotIn('material_upload', handler.call_args.args[1])

    async def test_real_dispatcher_routes_whole_album_and_repeat_without_network(self):
        class OfflineSession(BaseSession):
            def __init__(session):
                super().__init__()
                session.calls = []
                session.next_id = 5000

            async def close(session):
                pass

            async def stream_content(session, *args, **kwargs):
                raise AssertionError('Downloads forbidden in offline test')
                yield b''

            async def make_request(session, actual_bot, method, timeout=None):
                session.calls.append(method)
                if method.__api_method__ == 'deleteMessage':
                    return True
                if method.__api_method__ not in {'sendMessage','sendPhoto','sendDocument','sendMediaGroup'}:
                    raise AssertionError(f'Unexpected API method {method.__api_method__}')
                count = len(method.media) if method.__api_method__ == 'sendMediaGroup' else 1
                result = []
                for _ in range(count):
                    session.next_id += 1
                    result.append(Message(message_id=session.next_id, date=0,
                                          chat=Chat(id=int(method.chat_id), type='private')))
                return result if method.__api_method__ == 'sendMediaGroup' else result[0]

        session = OfflineSession()
        actual_bot = Bot('987654321:OFFLINE_TEST_TOKEN', session=session)
        state = bot.dp.fsm.get_context(bot=actual_bot,chat_id=10,user_id=10)
        await self.start_upload(state)
        user = User(id=10, is_bot=False, first_name='Student', username='student')
        def update(number,group):
            return Update(update_id=number,message=Message(message_id=number,date=0,
                chat=Chat(id=10,type='private'),from_user=user,media_group_id=group,
                photo=[PhotoSize(file_id=f'file-{number}',file_unique_id=f'unique-{number}',width=100,height=100)],
                caption='Подпись второго фото' if number==2 else None))
        try:
            await asyncio.gather(bot.dp.feed_update(actual_bot,update(2,'real-album')),
                                 bot.dp.feed_update(actual_bot,update(1,'real-album')),
                                 bot.dp.feed_update(actual_bot,update(3,'other-album')),
                                 bot.dp.feed_update(actual_bot,update(4,'other-album')))
            self.assertEqual(len(self.materials()),2)
            sizes = [len(db.get_material_attachments(row['id'])) for row in self.materials()]
            self.assertEqual(sizes,[2,2])
            self.assertEqual(sum(m.__api_method__=='sendMediaGroup' for m in session.calls),2)
            await bot.dp.feed_update(actual_bot,update(1,'real-album'))
            self.assertEqual(len(self.materials()),2)
        finally:
            await state.clear()
            await actual_bot.session.close()

    async def test_album_title_state_and_caption_fallback(self):
        await self.start_upload()
        await self.state.set_state(bot.Form.waiting_for_material_title)
        await self.state.update_data(mat_title='Название заранее')
        row = await self.two_photos(caption=None)
        self.assertEqual(row['title'],'Название заранее')
        self.assertEqual(len(db.get_material_attachments(row['id'])),2)

    async def test_foreign_task_cannot_be_uploaded_and_invalid_attachment_rolls_back(self):
        await self.start_upload()
        await self.state.update_data(subject_id=self.subject+100)
        await self.deliver(self.message(1))
        self.assertEqual(self.materials(),[])
        self.assertIsNone(await self.state.get_state())
        self.assertIsNone(db.create_material_submission(20,'other',self.task,'Foreign','foreign',[]))

    async def test_migration_preserves_existing_records_and_is_repeatable(self):
        mid = db.add_material(10,'student',self.task,'solution','Legacy','file','document','filename')
        db.set_material_status(mid,'approved')
        with db.get_connection() as conn:
            before = [tuple(r) for r in conn.execute('SELECT * FROM materials')]
        # Build an actual pre-migration DB from the existing records, omitting new tables.
        legacy = Path(self.temp.name) / 'legacy.db'
        with db.get_connection() as source, sqlite3.connect(legacy) as target:
            script = '\n'.join(line for line in source.iterdump()
                              if not any(name in line for name in ('material_attachments','material_upload_events','material_moderation_messages')))
            target.executescript(script)
        original = db.DB_PATH
        try:
            db.DB_PATH = str(legacy)
            db.init_db()
            db.init_db()
            with db.get_connection() as conn:
                self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM materials')],before)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.get_material_attachments(mid),[dict(file_id='file',file_type='document')])
        finally:
            db.DB_PATH=original

    async def test_atomic_replay_in_sqlite_from_competing_workers(self):
        def save():
            return db.create_material_submission(10,'student',self.task,'Название','same-source',
                [dict(file_id='one',file_type='photo'),dict(file_id='two',file_type='photo')])
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(lambda _:save(),range(4)))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(len(self.materials()),1)
        self.assertEqual(len(db.get_material_attachments(self.materials()[0]['id'])),2)

    async def test_attachment_failure_rolls_back_material_and_event(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_material_submission(10,'student',self.task,'Название','rollback',
                                          [dict(file_id=None,file_type='photo')])
        self.assertEqual(self.materials(),[])
        self.assertFalse(db.material_upload_seen('rollback'))


if __name__ == '__main__':
    unittest.main()
