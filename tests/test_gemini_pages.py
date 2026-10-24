import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import gdrive_sync
import schedule_review as review


def lesson(numbers, subject='Дизайн', subgroup=None):
    return dict(lesson_numbers=numbers, subject=subject, teachers=['Преподаватель'],
                subgroup=subgroup, cancelled=False, uncertainties=[])


def group(rows, name='ТТ-36'):
    return dict(group_name=name, group_found=True, lessons=rows, uncertainties=[])


def page(groups, context=False, complete=True):
    return dict(groups=groups, uncertainties=[], coverage_complete=complete, requires_context=context)


def response(data):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(data)}]}}]}


class GeminiPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, db.DB_PATH = db.DB_PATH, str(Path(self.tmp.name) / 'test.db')
        db.init_db()
        self.date = gdrive_sync.get_target_study_date()[0].isoformat()
        self.sources = []
        for index in (1,2):
            path = Path(self.tmp.name) / f'secret-path-{index}.jpg'
            path.write_bytes(f'photo{index}'.encode())
            self.sources.append(dict(source=str(path), caption=f'Замены: {self.date}, часть {index}'))
        self.env = patch.dict(os.environ, {'GEMINI_API_KEY':'fixture-secret', 'GEMINI_MODEL':'fixture-model'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        db.DB_PATH = self.old_db
        self.tmp.cleanup()

    def prepare(self, replies):
        with patch.object(gdrive_sync, 'drive_schedule_payload', return_value={'photos':self.sources}), \
                patch.object(review, 'check_connection'), \
                patch.object(review, '_gemini_json_request', side_effect=replies) as request:
            review.prepare_reviews()
        return request

    def test_group_split_between_pages_preserves_numbers_and_repeated_text(self):
        request = self.prepare([response(page([group([lesson([1,2])])])),
                                response(page([group([lesson([3,4])])]))])
        drafts = review.pending_reviews()
        self.assertEqual(len(drafts), 1)
        self.assertTrue(all(call.args[2] is None for call in request.call_args_list))
        self.assertEqual(drafts[0]['body'], '1-2 Дизайн (Преподаватель)\n3-4 Дизайн (Преподаватель)')
        self.assertEqual([len(json.loads(c.args[0].data)['contents'][0]['parts']) for c in request.call_args_list], [2,2])

    def test_overlapping_duplicates_require_whole_group_context(self):
        row = lesson([1,2])
        request = self.prepare([response(page([group([row])])), response(page([group([row])])),
                                response(group([row]))])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(review.pending_reviews()[0]['body'].count('Дизайн'), 1)
        context = json.loads(request.call_args.args[0].data)
        self.assertEqual(len(context['contents'][0]['parts']), 3)
        self.assertIn('ТОЛЬКО группу ТТ-36', context['contents'][0]['parts'][0]['text'])

    def test_overlap_with_distinct_subgroups_is_not_deduplicated_as_text(self):
        a, b = lesson([1],subgroup='а'), lesson([1],subgroup='б')
        self.prepare([response(page([group([a])])), response(page([group([b])])), response(group([a,b]))])
        body = review.pending_reviews()[0]['body']
        self.assertIn('«а»', body)
        self.assertIn('«б»', body)

    def test_unassigned_continuation_falls_back_to_document_context(self):
        request = self.prepare([response(page([group([lesson([1])])])),
                                response(page([], context=True, complete=False)),
                                response({'groups':[group([lesson([1]),lesson([2])])], 'uncertainties':[]})])
        self.assertEqual(request.call_count, 3)
        self.assertIn('2 Дизайн', review.pending_reviews()[0]['body'])
        self.assertIn('ВСЕ группы', json.loads(request.call_args.args[0].data)['contents'][0]['parts'][0]['text'])

    def test_page_failure_has_no_drafts_and_retry_reuses_success_after_restart(self):
        with self.assertRaisesRegex(review.GeminiError, '1 из 2'):
            self.prepare([response(page([group([lesson([1])])])), review.GeminiError('Google не ответил')])
        self.assertEqual(review.pending_reviews(), [])
        with db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schedule_gemini_batches').fetchone()[0], 0)
        db.init_db()
        request = self.prepare([response(page([group([lesson([2])])]))])
        self.assertEqual(request.call_count, 1)
        self.assertIn('1 Дизайн', review.pending_reviews()[0]['body'])
        self.assertIn('2 Дизайн', review.pending_reviews()[0]['body'])

    def test_context_failure_retains_pages_and_retries_only_reconciliation(self):
        row = lesson([1])
        with self.assertRaisesRegex(review.GeminiError, 'объединение не завершено'):
            self.prepare([response(page([group([row])])), response(page([group([row])])), review.GeminiError('HTTP 503')])
        self.assertEqual(review.pending_reviews(), [])
        request = self.prepare([response(group([row]))])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(review.pending_reviews()), 1)

    def test_group_context_must_confirm_presence(self):
        row = lesson([1])
        with self.assertRaisesRegex(review.GeminiError, 'продолжение группы'):
            self.prepare([response(page([group([row])])), response(page([group([row])])),
                          response(dict(group_found=False))])
        self.assertEqual(review.pending_reviews(), [])

    def test_numeric_source_order_is_preserved(self):
        self.sources[0]['caption'] = f'Замены: {self.date}, часть 10'
        self.sources[1]['caption'] = f'Замены: {self.date}, часть 9'
        request = self.prepare([response(page([group([lesson([1])])])), response(page([group([lesson([2])])]))])
        first_request = json.loads(request.call_args_list[0].args[0].data)
        image = first_request['contents'][0]['parts'][1]['inlineData']['data']
        self.assertEqual(base64.b64decode(image), b'photo2')

    def test_missing_page_cannot_silently_create_partial_draft(self):
        self.sources[1]['source'] = str(Path(self.tmp.name) / 'not-downloaded.jpg')
        with self.assertRaisesRegex(review.GeminiError, 'недоступна'):
            self.prepare([])
        self.assertEqual(review.pending_reviews(), [])

    def test_page_requires_explicit_coverage_confirmation(self):
        for incomplete in ({'groups':[group([lesson([1])])]}, page([group([lesson([1])])], complete=False)):
            with self.subTest(incomplete=incomplete), self.assertRaises(review.GeminiError):
                self.prepare([response(incomplete)])
            self.assertEqual(review.pending_reviews(), [])

    def test_prepared_batch_preserves_edits_and_publications(self):
        self.prepare([response(page([group([lesson([1])])])), response(page([group([lesson([2])])]))])
        draft = review.pending_reviews()[0]
        review.edit_review(draft['id'], 'Исправлено вручную')
        review.finish_review(draft['id'], True)
        request = self.prepare([])
        self.assertEqual(request.call_count, 0)
        self.assertEqual(review.get_review(draft['id'])['body'], 'Исправлено вручную')
        self.assertEqual(review.get_review(draft['id'])['status'], 'published')

    def test_changed_page_only_is_reextracted(self):
        self.prepare([response(page([group([lesson([1])])])), response(page([group([lesson([2])])]))])
        Path(self.sources[1]['source']).write_bytes(b'changed')
        request = self.prepare([response(page([group([lesson([3])])]))])
        self.assertEqual(request.call_count, 1)
        body = review.pending_reviews()[0]['body']
        self.assertIn('1 Дизайн', body)
        self.assertNotIn('2 Дизайн', body)
        self.assertIn('3 Дизайн', body)

    def test_safe_diagnostics_include_metrics_but_no_content_or_credentials(self):
        result = response(page([group([lesson([1],subject='private-subject')])]))
        with self.assertLogs(review.logger, level='INFO') as logs, patch.object(review.urllib.request, 'urlopen',
                return_value=io.BytesIO(json.dumps(result).encode())):
            review.read_schedule([self.sources[0]['source']], None, page_context=(1,2))
        text = '\n'.join(logs.output)
        for metric in ('model=fixture-model','count=1','image_bytes=','request_bytes=','duration='):
            self.assertIn(metric, text)
        for private in ('fixture-secret','private-subject','secret-path','Преподаватель'):
            self.assertNotIn(private, text)

    def test_legacy_document_cache_preserves_existing_publication(self):
        content = [{'caption': photo['caption'], 'sha256':hashlib.sha256(Path(photo['source']).read_bytes()).hexdigest()}
                   for photo in self.sources]
        fingerprint = 'gemini-v1:' + hashlib.sha256(json.dumps(
            sorted(content, key=lambda item:(item['caption'],item['sha256'])), sort_keys=True).encode()).hexdigest()
        with db.get_connection() as conn:
            conn.execute('CREATE TABLE schedule_gemini_batches (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            conn.execute('INSERT INTO schedule_gemini_batches VALUES (?,?)', (fingerprint,json.dumps({'ТТ-36':'Старый готовый результат'})))
        self.prepare([])
        draft = review.pending_reviews()[0]
        review.edit_review(draft['id'], 'Ручная правка прежней версии')
        review.finish_review(draft['id'], True)
        self.assertEqual(self.prepare([]).call_count, 0)
        self.assertEqual(review.get_review(draft['id'])['status'], 'published')
        self.assertEqual(review.get_review(draft['id'])['body'], 'Ручная правка прежней версии')

    def test_gap_in_numbered_pages_is_not_treated_as_complete_document(self):
        self.sources[1]['caption'] = f'Замены: {self.date}, часть 3'
        with self.assertRaisesRegex(review.GeminiError, 'пропущена страница'):
            self.prepare([])
        self.assertEqual(review.pending_reviews(), [])

    def test_ambiguous_page_order_requires_document_context(self):
        self.sources[1]['caption'] = self.sources[0]['caption']
        request = self.prepare([response(page([group([lesson([1])])])), response(page([group([lesson([2])])])),
                                response({'groups':[group([lesson([1]),lesson([2])])], 'uncertainties':[]})])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(review.pending_reviews()), 1)
