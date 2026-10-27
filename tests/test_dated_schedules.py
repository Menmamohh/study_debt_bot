import asyncio
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot
import database as db
import dated_schedules as ds
import gdrive_sync
import schedule_review
from dated_schedule_ui import DatedForm

FRI, SAT = '2026-10-30', '2026-10-31'


def op(day=FRI, photo='p1', groups=None, **kw):
    return dict(date=day,date_confirmed=True,photo=photo,body='',received_at='2026-10-30T09:00:00Z',
                label='Лист',groups=groups or ['ALL'],**kw)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name)/'test.db')
        db.init_db()
        for uid,group,status in [(1,'ТТ-36','approved'),(2,'ТТ-37','approved'),(3,'ТТ-36','pending')]:
            db.add_user_request(uid,None,'S','S',group)
            db.set_user_status(uid,status)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_friday_upload_for_two_days_atomic_and_independent(self):
        result=ds.publish('two',[op(),op(SAT,'s1')],'admin')
        self.assertEqual(result['dates'],[FRI,SAT])
        self.assertEqual(ds.sheets(FRI,'ТТ-36')[0]['photo'],'p1')
        self.assertEqual(ds.sheets(SAT,'ТТ-36')[0]['photo'],'s1')
        self.assertEqual(ds.dates('ТТ-36'),[SAT,FRI])
        with db.get_connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM dated_publications').fetchone()[0],2)
        deliveries=schedule_review.pending_deliveries()
        self.assertEqual(len(deliveries),4)
        self.assertEqual({d['callback_data'] for d in deliveries},{f'dsv:{FRI}:ALL',f'dsv:{SAT}:ALL'})
        self.assertEqual({d['user_id'] for d in deliveries},{1,2})

    def test_group_patch_preserves_other_sheets_dates_order_and_history(self):
        initial=ds.publish('first',[op(),op(photo='p2'),op(SAT,'sat')],'a')
        before=ds.sheets(FRI,'ТТ-36')
        sid=before[0]['id']
        patch_op=op(photo='fixed',groups=['ТТ-36'],sheet_id=sid,expected=ds.expected_heads(sid,['ТТ-36']))
        ds.publish('patch',[patch_op],'a')
        self.assertEqual([r['photo'] for r in ds.sheets(FRI,'ТТ-36')],['fixed','p2'])
        self.assertEqual([r['photo'] for r in ds.sheets(FRI,'ТТ-37')],['p1','p2'])
        self.assertEqual([r['id'] for r in before],[r['id'] for r in ds.sheets(FRI,'ТТ-36')])
        self.assertEqual(ds.sheets(SAT,'ТТ-36')[0]['photo'],'sat')
        self.assertEqual(ds.sheets(FRI,'ТТ-36',initial['batch'])[0]['photo'],'p1')
        latest=[x for x in schedule_review.pending_deliveries() if x['event_key'].startswith('dated:2:')]
        self.assertEqual([x['user_id'] for x in latest],[1])

    def test_repeat_confirmation_and_retry_after_restart(self):
        first=ds.publish('token',[op()],'a')
        db.init_db()
        repeated=ds.publish('token',[op()],'a')
        self.assertFalse(repeated['changed'])
        self.assertEqual(first['batch'],repeated['batch'])
        self.assertEqual(len(ds.publications(FRI,'ТТ-36')),1)
        self.assertEqual(len(schedule_review.pending_deliveries()),2)

    def test_unconfirmed_date_and_scope_are_rejected_without_partial_publish(self):
        bad=op(SAT);bad['date_confirmed']=False
        with self.assertRaises(ValueError): ds.publish('bad',[op(),bad],'a')
        self.assertEqual(ds.dates('ТТ-36'),[])
        bad=op();bad['groups']=[]
        with self.assertRaises(ValueError): ds.publish('bad',[bad],'a')
        self.assertEqual(schedule_review.pending_deliveries(),[])

    def test_stale_replacement_rolls_back_entire_batch(self):
        ds.publish('one',[op()],'a');sid=ds.sheets(FRI,'ТТ-36')[0]['id']
        stale=op(photo='stale',groups=['ТТ-36'],sheet_id=sid,expected=ds.expected_heads(sid,['ТТ-36']))
        ds.publish('fresh',[dict(stale,photo='fresh')],'a')
        with self.assertRaises(ds.Conflict):ds.publish('stale',[op(SAT),stale],'a')
        self.assertEqual(ds.dates('ТТ-36'),[FRI])
        self.assertEqual(ds.sheets(FRI,'ТТ-36')[0]['photo'],'fresh')

    def test_replace_date_cannot_move_sheet(self):
        ds.publish('one',[op()],'a');sid=ds.sheets(FRI,'ТТ-36')[0]['id']
        with self.assertRaises(ds.Conflict):ds.publish('two',[op(SAT,sheet_id=sid,expected={})],'a')
        self.assertEqual(ds.dates('ТТ-36'),[FRI])

    def test_overlapping_replacements_rejected(self):
        ds.publish('one',[op()],'a');sid=ds.sheets(FRI,'ТТ-36')[0]['id']
        one=op(groups=['ТТ-36'],sheet_id=sid,expected=ds.expected_heads(sid,['ТТ-36']))
        with self.assertRaises(ds.Conflict):ds.publish('bad',[one,one],'a')

    def test_changed_drive_file_never_overwrites_manual_and_retains_bytes(self):
        source=Path(self.tmp.name)/'sheet.jpg';source.write_bytes(b'old')
        ds.publish('one',[op(photo=str(source),source_key='drive:one:hash')],'a')
        retained=ds.sheets(FRI,'ТТ-36')[0]['photo']
        file=dict(file_id='one',filename='30-10-2026.jpg',doc_type='replacement',schedule_date=FRI,
                  part_number=1,drive_timestamp=2,file_size=3)
        def download(fid,path):Path(path).write_bytes(b'new');return True
        with patch.object(gdrive_sync,'DOWNLOADS_DIR',str(Path(self.tmp.name)/'download')), \
             patch.object(gdrive_sync,'fetch_drive_files_list',return_value=[file]), \
             patch.object(gdrive_sync,'download_file',side_effect=download), \
             patch.object(schedule_review,'prepare_reviews') as recognize:
            gdrive_sync.sync_drive_schedule()
            gdrive_sync.sync_drive_schedule(force=True)
            recognize.assert_not_called()
        self.assertEqual(Path(retained).read_bytes(),b'old')
        self.assertEqual(ds.sheets(FRI,'ТТ-36')[0]['photo'],retained)
        self.assertEqual(len(ds.publications(FRI,'ТТ-36')),1)
        self.assertEqual(len(ds.drive_candidates()),1)

    def test_noop_sync_keeps_received_date(self):
        db.save_gdrive_file('f','x','replacement',FRI,file_size=3,drive_timestamp=1)
        with db.get_connection() as c:c.execute("UPDATE gdrive_schedule_files SET downloaded_at='2026-10-01'")
        db.save_gdrive_file('f','renamed','replacement',SAT,file_size=3,drive_timestamp=1)
        self.assertEqual(db.get_gdrive_file('f')['downloaded_at'],'2026-10-01')
        self.assertEqual(db.get_gdrive_file('f')['schedule_date'],SAT)

    def test_migration_keeps_legacy_unknown_dates_and_is_repeatable_with_backup(self):
        legacy=str(Path(self.tmp.name)/'legacy.db')
        with sqlite3.connect(legacy) as c:
            c.executescript('''CREATE TABLE schedule_versions(id INTEGER PRIMARY KEY,group_name TEXT,fingerprint TEXT,payload TEXT,created_at TEXT);
                CREATE TABLE group_schedules(group_name TEXT PRIMARY KEY,title TEXT,schedule_text TEXT,photo_file_id TEXT,updated_at TEXT,updated_by TEXT);''')
            c.execute('INSERT INTO schedule_versions VALUES (1,?,?,?,?)',('ALL','x',json.dumps({'photos':[{'source':'old','caption':'Замены: '+FRI}]}),'2026-10-30 10:00:00'))
            c.execute('INSERT INTO group_schedules VALUES (?,?,?,?,?,?)',('ТТ-36','Без даты','старое','old', '2026-10-29','a'))
        db.DB_PATH=legacy
        db.init_db();db.init_db()
        self.assertTrue(Path(legacy+'.before-dated-v1.bak').is_file())
        self.assertEqual(ds.sheets(FRI,'ТТ-36')[0]['photo'],'old')
        self.assertEqual(db.get_group_schedule('ТТ-36')['schedule_text'],'старое')
        self.assertEqual(len(ds.publications(FRI,'ТТ-36')),1)
        with sqlite3.connect(legacy+'.before-dated-v1.bak') as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM schedule_versions').fetchone()[0],1)
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='dated_sheets'").fetchone())

    def test_dated_drive_payload_contains_both_days(self):
        for day in (FRI,SAT):
            photo=Path(self.tmp.name)/(day+'.jpg');photo.write_bytes(day.encode())
            db.save_gdrive_file(day,photo.name,'replacement',day,local_path=str(photo))
        self.assertEqual(len(gdrive_sync.drive_schedule_payload()['photos']),2)

    def test_friday_afternoon_recognition_keeps_friday_and_saturday(self):
        sources=[]
        for day in (FRI,SAT):
            path=Path(self.tmp.name)/(day+'.jpg');path.write_bytes(day.encode())
            sources.append({'source':str(path),'caption':f'Замены: {day}, часть 1'})
        with patch.object(schedule_review,'review_min_date',return_value=FRI), \
             patch.object(gdrive_sync,'get_target_study_date',return_value=(dt.date.fromisoformat(SAT),True,'')), \
             patch.object(gdrive_sync,'drive_schedule_payload',return_value={'photos':sources}), \
             patch.object(schedule_review,'configured',return_value=False), \
             patch.object(gdrive_sync,'_schedule_group_markers',return_value=(1,1,(('ТТ-36',0),))), \
             patch.object(gdrive_sync,'_ocr_schedule_group',return_value='Текст'):
            schedule_review.prepare_reviews()
            self.assertEqual({d['schedule_date'] for d in schedule_review.pending_reviews()},{FRI,SAT})

    def test_hints_do_not_assign_effective_date(self):
        self.assertEqual(ds.suggest_date('лист 31.10.2026'),'2026-10-31')
        self.assertIsNone(ds.suggest_date('32.10.2026'))
        self.assertIsNone(ds.suggest_date('расписание'))

    def test_same_confirmation_from_two_workers_commits_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:ds.publish('concurrent',[op()],'a'),range(2)))
        self.assertEqual(sum(result['changed'] for result in results),1)
        self.assertEqual(len(ds.publications(FRI,'ТТ-36')),1)
        self.assertEqual(len(schedule_review.pending_deliveries()),2)

    def test_notifications_only_one_per_date_and_user_for_overlapping_scopes(self):
        ds.publish('two',[op(),op(photo='p2',groups=['ТТ-36'])],'a')
        self.assertEqual(len(schedule_review.pending_deliveries()),2)

    def test_new_ocr_draft_keeps_confirmed_text(self):
        with db.get_connection() as c:
            c.execute("INSERT INTO schedule_reviews(group_name,schedule_date,fingerprint,sources,body,status) VALUES (?,?,?,?,?,?)",
                      ('ТТ-36',FRI,'old','[]','confirmed','published'))
            c.execute("INSERT INTO schedule_reviews(group_name,schedule_date,fingerprint,sources,body) VALUES (?,?,?,?,?)",
                      ('ТТ-36',FRI,'new','[]','unconfirmed'))
        self.assertEqual(schedule_review.approved_schedule('ТТ-36',dt.date.fromisoformat(FRI))['body'],'confirmed')
        self.assertEqual(schedule_review.next_approved_schedule('ТТ-36',dt.date.fromisoformat(FRI))['body'],'confirmed')


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=db.DB_PATH;db.DB_PATH=str(Path(self.tmp.name)/'test.db');db.init_db()
        self.state=FSMContext(MemoryStorage(),StorageKey(bot_id=1,chat_id=99,user_id=99))
        self.telegram=NS(send_photo=AsyncMock(return_value=NS(message_id=11)),send_message=AsyncMock(return_value=NS(message_id=12)))
        self.cb=NS(data='ds:start',from_user=NS(id=99),message=NS(message_id=1,chat=NS(type='private',id=99)),bot=self.telegram,answer=AsyncMock())
        self.msg=NS(from_user=self.cb.from_user,chat=self.cb.message.chat,bot=self.telegram,photo=[NS(file_id='new')],caption='Лист на 31.10.2026',text=None)
        self.patches=[patch.object(bot,'is_admin',return_value=True),patch.object(bot,'show_user_screen',AsyncMock()),
                      patch.object(bot,'deliver_schedule_notifications',AsyncMock(return_value=(0,0)))]
        for p in self.patches:p.start()
        bot.dated_ui.lock=None

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        db.DB_PATH=self.old;self.tmp.cleanup()

    async def click(self,action,arg=''):
        data=await self.state.get_data();self.cb.data=f'dse:{data["ds_token"]}:{action}:{arg}'
        await bot.dated_ui.callback(self.cb,self.state)

    async def test_upload_needs_date_and_groups_before_publish(self):
        await bot.dated_ui.start(self.cb,self.state)
        await bot.process_group_schedule_input(self.msg,self.state,self.telegram)
        data=await self.state.get_data()
        self.assertFalse(data['ds_ops'][0]['date_confirmed'])
        self.assertEqual(data['ds_ops'][0]['suggested_date'],SAT)
        await self.click('publish')
        self.assertEqual(ds.dates('ТТ-36'),[])
        await self.click('day',SAT);await self.click('group','ТТ-36');await self.click('preview')
        self.assertIn(SAT,bot.show_user_screen.call_args.args[1])
        await self.click('publish')
        self.assertEqual(ds.sheets(SAT,'ТТ-36')[0]['photo'],'new')
        self.assertEqual(ds.sheets(SAT,'ТТ-37'),[])

    async def test_cancel_and_stale_confirm_never_publish(self):
        await bot.dated_ui.start(self.cb,self.state)
        await bot.process_group_schedule_input(self.msg,self.state,self.telegram)
        token=(await self.state.get_data())['ds_token']
        self.cb.data='ds:cancel';await bot.dated_ui.callback(self.cb,self.state)
        self.cb.data=f'dse:{token}:publish:';await bot.dated_ui.callback(self.cb,self.state)
        self.assertEqual(ds.dates('ТТ-36'),[])
        self.assertEqual(await self.state.get_data(),{})

    async def test_date_input_and_next_sheet(self):
        await bot.dated_ui.start(self.cb,self.state)
        await bot.process_group_schedule_input(self.msg,self.state,self.telegram,[self.msg,self.msg])
        await self.click('date')
        self.msg.text='30.10.2026';await bot.dated_ui.date_input(self.msg,self.state)
        await self.click('all');await self.click('next');await self.click('day',SAT);await self.click('all')
        await self.click('preview');await self.click('publish')
        self.assertEqual(ds.dates('ТТ-36'),[SAT,FRI])

    async def test_notification_opens_friday_after_saturday_published(self):
        ds.publish('one',[op()],'a');ds.publish('two',[op(SAT,'sat')],'a')
        self.cb.data=f'dsv:{FRI}:ТТ-36'
        with patch.object(bot,'check_user_access',AsyncMock(return_value=(True,''))):
            await bot.dated_ui.callback(self.cb,self.state)
        self.assertIn(FRI,bot.show_user_screen.call_args.args[1])
        self.assertEqual(self.telegram.send_photo.call_args.kwargs['photo'],'p1')
        buttons=bot.show_user_screen.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(b.callback_data==f'dsv:{SAT}:ТТ-36' for row in buttons for b in row))

    async def test_replace_shows_old_and_new_and_scope(self):
        ds.publish('one',[op(),op(photo='p2')],'a');sid=ds.sheets(FRI,'ТТ-36')[0]['id']
        await bot.dated_ui.start(self.cb,self.state,sheet_id=sid)
        await bot.process_group_schedule_input(self.msg,self.state,self.telegram)
        await self.click('group','ТТ-36');await self.click('compare')
        self.assertEqual([x.kwargs['photo'] for x in self.telegram.send_photo.call_args_list[-2:]],['p1','new'])
        await self.click('preview');await self.click('publish')
        self.assertEqual([x['photo'] for x in ds.sheets(FRI,'ТТ-37')],['p1','p2'])

    async def test_changed_scope_requires_new_preview_before_confirmation(self):
        await bot.dated_ui.start(self.cb,self.state)
        await bot.process_group_schedule_input(self.msg,self.state,self.telegram)
        await self.click('day',SAT);await self.click('all');await self.click('preview')
        await self.click('group','ТТ-36')
        await self.click('publish')
        self.assertEqual(ds.dates('ТТ-36'),[])
        await self.click('publish')
        self.assertEqual(ds.dates('ТТ-36'),[SAT])
        self.assertEqual(ds.dates('ТТ-37'),[])

    async def test_unauthorized_admin_action_is_rejected(self):
        with patch.object(bot,'is_admin',return_value=False):
            await bot.dated_ui.callback(self.cb,self.state)
        self.assertEqual(await self.state.get_data(),{})
        self.cb.answer.assert_awaited()

    async def test_related_photos_preserved_until_new_screen_succeeds(self):
        await bot.dated_ui.screen(self.cb,'new',[[('Главное меню','main_menu')]], [('p1','old'),('p2','new')])
        self.assertEqual(bot.show_user_screen.call_args.kwargs['keep_message_ids'],[11,11])
        with patch.object(bot,'show_user_screen',AsyncMock(side_effect=RuntimeError('network'))), \
             patch.object(bot,'cleanup_user_ui_messages',AsyncMock()) as clean:
            with self.assertRaises(RuntimeError):await bot.dated_ui.screen(self.cb,'new',[],[('p1','new')])
            clean.assert_not_awaited()

    async def test_review_requires_new_date_preview_and_cancel_preserves_published(self):
        draft=dict(id=1,body='проверенный текст',created_at='2026-10-30',schedule_date=FRI,group_name='ТТ-36')
        await bot.dated_ui.import_review(self.cb,self.state,draft)
        self.assertFalse((await self.state.get_data())['ds_ops'][0]['date_confirmed'])
        self.assertEqual(ds.dates('ТТ-36'),[])
        self.cb.data='ds:cancel';await bot.dated_ui.callback(self.cb,self.state)
        self.assertEqual(ds.dates('ТТ-36'),[])

    async def test_legacy_archive_and_back_buttons(self):
        db.set_group_schedule('ТТ-36','Old undated')
        self.cb.data='dsl:ТТ-36'
        with patch.object(bot,'check_user_access',AsyncMock(return_value=(True,''))), \
             patch.object(bot,'render_group_schedule_view',AsyncMock()) as legacy:
            await bot.dated_ui.callback(self.cb,self.state)
        self.assertTrue(legacy.call_args.kwargs['legacy_only'])


if __name__=='__main__':unittest.main()
