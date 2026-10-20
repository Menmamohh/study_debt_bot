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

    def test_network_failures_are_distinct_and_do_not_echo_secrets(self):
        import socket
        import ssl
        request = gemini.urllib.request.Request('https://example.invalid')
        cases = [(urllib.error.URLError(socket.gaierror('secret')), 'DNS'),
                 (urllib.error.URLError(ssl.SSLCertVerificationError('secret')), 'TLS'),
                 (urllib.error.URLError(TimeoutError('secret')), 'ожидания'),
                 (TimeoutError('secret'), '20 с'),
                 (urllib.error.URLError('secret'), 'соединение')]
        for exception, expected in cases:
            with self.subTest(expected=expected), patch.object(gemini.urllib.request, 'urlopen', side_effect=exception):
                with self.assertRaises(gemini.GeminiError) as error:
                    gemini._gemini_json_request(request, 'Проверка', 20)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn('secret', str(error.exception))
        with patch.object(gemini.urllib.request, 'urlopen', return_value=io.BytesIO(b'not json secret')):
            with self.assertRaisesRegex(gemini.GeminiError, 'некорректный JSON'):
                gemini._gemini_json_request(request, 'Проверка', 20)

    def test_preflight_sends_no_photos_and_checks_model(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'secret'}), patch.object(
                gemini.urllib.request, 'urlopen', return_value=io.BytesIO(
                    b'{"supportedGenerationMethods":["generateContent"]}')) as send:
            self.assertTrue(gemini.check_connection())
            request = send.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            self.assertNotIn('secret', request.full_url)

    def test_invalid_key_is_reported_without_remote_message(self):
        body = json.dumps({'error': {'message': 'secret', 'details': [{'reason': 'API_KEY_INVALID'}]}}).encode()
        error = urllib.error.HTTPError('url', 400, 'secret', {}, io.BytesIO(body))
        with patch.object(gemini.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(gemini.GeminiError, 'недействителен') as result:
                gemini._gemini_json_request(gemini.urllib.request.Request('https://example.invalid'), 'Доступ', 20)
            self.assertNotIn('secret', str(result.exception))
