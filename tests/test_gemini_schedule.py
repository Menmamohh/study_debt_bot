import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import schedule_review as gemini


class GeminiTests(unittest.TestCase):
    def data(self):
        return {'group_found': True, 'lessons': [
            {'lesson_numbers': [1, 2], 'subject': 'Дизайн', 'teachers': ['Иванов И.И.'],
             'subgroup': None, 'cancelled': False, 'uncertainties': []},
            {'lesson_numbers': [3], 'subject': None, 'teachers': [],
             'subgroup': None, 'cancelled': True, 'uncertainties': []}], 'uncertainties': []}

    def test_format_and_validation(self):
        self.assertEqual(gemini.format_result(self.data()), '1-2 Дизайн (Иванов И.И.)\n3 Сняты')
        data = self.data(); data['lessons'][0]['lesson_numbers'] = ['1-2']
        with self.assertRaises(gemini.GeminiError): gemini.format_result(data)
        data = self.data(); data['lessons'][0]['uncertainties'] = ['Проверить фамилию']
        self.assertIn('Проверить фамилию', gemini.format_result(data))

    def test_request_reads_full_photos_and_uses_header_key(self):
        reply = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': json.dumps(self.data())}]}}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'photo.jpg'; path.write_bytes(b'photo')
            with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-secret'}), \
                    patch.object(gemini.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(reply).encode())) as send:
                result = gemini.read_schedule([path, path], 'ТТ-36')
            self.assertIn('Дизайн', result['text'])
            request = send.call_args.args[0]
            self.assertNotIn('test-secret', request.full_url)
            self.assertEqual(len(json.loads(request.data)['contents'][0]['parts']), 3)

    def test_quota_error_is_safe_and_incomplete_output_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'photo.jpg'; path.write_bytes(b'photo')
            with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-secret'}), \
                    patch.object(gemini.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(
                        'url', 429, 'test-secret', {}, None)):
                with self.assertRaises(gemini.GeminiError) as error: gemini.read_schedule([path], 'ТТ-36')
                self.assertNotIn('test-secret', str(error.exception))
            reply = {'candidates': [{'finishReason': 'MAX_TOKENS'}]}
            with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-secret'}), \
                    patch.object(gemini.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(reply).encode())):
                with self.assertRaises(gemini.GeminiError): gemini.read_schedule([path], 'ТТ-36')
