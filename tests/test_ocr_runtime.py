import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import gdrive_sync as sync


class OcrRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='ocr-runtime-'))
        self.image = self.folder / 'sheet.png'
        Image.new('RGB', (900, 1200), 'white').save(self.image)
        sync._schedule_group_markers.cache_clear()
        sync._ocr_schedule_group.cache_clear()

    def test_server_budget_used_for_group_detection_and_reading(self):
        tsv = (b'level\tpage_num\tblock_num\tpar_num\tline_num\ttop\tconf\ttext\n'
               + '5\t1\t1\t1\t1\t100\t90\tТТ-36\n'.encode())
        with patch.object(sync.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, tsv, b'')) as run:
            text = sync._ocr_schedule_group(str(self.image), 0, 'ТТ-36')
        self.assertEqual(text, '')
        self.assertEqual(run.call_count, 4)
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [60, 60, 60, 60])

    def test_detection_timeout_is_handled(self):
        with patch.object(sync.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tesseract', 60)):
            self.assertEqual(sync._ocr_schedule_group(str(self.image), 0, 'ТТ-36'), '')

    def test_group_reading_timeout_is_handled(self):
        with patch.object(sync, '_schedule_group_markers', return_value=(900, 1200, (('ТТ-36', 100),))), \
                patch.object(sync.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tesseract', 60)):
            self.assertEqual(sync._ocr_schedule_group(str(self.image), 0, 'ТТ-36'), '')

    def test_column_crop_and_coordinates_are_preserved(self):
        import io
        tsv = (b'level\tpage_num\tblock_num\tpar_num\tline_num\ttop\tconf\ttext\n'
               + '5\t1\t1\t1\t1\t300\t90\tТТ-36\n'.encode()
               + '5\t1\t1\t1\t2\t900\t90\tТТ-37\n'.encode())
        with patch.object(sync.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, tsv, b'')) as run:
            result = sync._schedule_group_markers(str(self.image), 0)
        self.assertEqual(result, (900, 1200, (('ТТ-36', 100), ('ТТ-37', 300))))
        with Image.open(io.BytesIO(run.call_args.kwargs['input'])) as column:
            self.assertEqual(column.size, (513, 3600))

    def test_group_number_is_matched_as_a_whole_label(self):
        tsv = (b'level\tpage_num\tblock_num\tpar_num\tline_num\ttop\tconf\ttext\n'
               + '5\t1\t1\t1\t1\t300\t90\tТТ-368\n'.encode())
        with patch.object(sync.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, tsv, b'')) as run:
            self.assertEqual(sync._ocr_schedule_group(str(self.image), 0, 'ТТ-36'), '')
        self.assertEqual(run.call_count, 1)

    def test_tsv_is_formatted_as_lesson_subject_and_teacher(self):
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        entries = [
            (5, 1, 1, 1, 1, 340, 100, 32, 18, 91, '1,2'),
            (5, 1, 1, 1, 1, 420, 100, 180, 18, 91, 'Математика'),
            (5, 1, 1, 1, 1, 1050, 100, 115, 18, 91, 'Иванова'),
            (5, 1, 1, 1, 2, 340, 130, 12, 18, 91, '3'),
            (5, 1, 1, 1, 2, 420, 130, 200, 18, 91, 'Физика'),
            (5, 1, 1, 1, 2, 1050, 130, 115, 18, 91, 'Петров'),
            (5, 1, 1, 1, 3, 340, 160, 32, 18, 91, '6,7'),
            (5, 1, 1, 1, 3, 420, 160, 210, 18, 91, 'Дизайн мебели'),
            (5, 1, 1, 1, 3, 1050, 160, 115, 18, 91, 'Чернышев'),
            (5, 1, 1, 1, 4, 420, 190, 210, 18, 91, 'Экономика'),
            (5, 1, 1, 1, 4, 1050, 190, 115, 18, 91, 'Шенок'),
        ]
        tsv = header + ''.join('\t'.join(map(str, row)) + '\n' for row in entries)
        self.assertEqual(
            sync._format_schedule_ocr(tsv, 1650),
            '1-2 Математика (Иванова)\n'
            '3 Физика (Петров)\n'
            '6-7 Дизайн мебели (Чернышев)\n'
            '6-7 Экономика (Шенок)',
        )

    def test_real_photo_recognition_rows(self):
        fixture = Path(__file__).parent / 'fixtures' / 'schedule_tt36.tsv'
        result = sync._format_schedule_ocr(fixture.read_text(), 3400)
        self.assertEqual(len(result.splitlines()), 6)
        self.assertIn('0-1 Сняты', result)
        self.assertIn('3 Охрана окружающей среды и энергосбережения (Чкалова А.В.)', result)
        self.assertIn('4 Основы автоматизации производства (Чванькова Н.В.)', result)
        self.assertIn('5-6 Физическая культура и здоровье', result)
        self.assertNotIn('Заместитель', result)
