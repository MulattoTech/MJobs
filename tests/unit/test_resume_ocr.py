"""Bounded local OCR uses generated scans, never a private resume or remote API."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image
from fixtures.resume_scans import scanned_resume

from services.career_profile.extraction import extract_resume
from services.career_profile import ocr


class ResumeOCRTests(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tool(self, executable, args, timeout):
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, ocr.PROCESS_TIMEOUT)
        if executable.endswith('pdftoppm'):
            prefix = Path(args[-1]); self.paths.append(prefix.parent)
            self.assertIn('-scale-to', args)
            self.assertEqual(args[args.index('-scale-to')+1], str(ocr.MAX_DIMENSION))
            Image.new('RGB', (100, 100), 'white').save(str(prefix)+'.png')
        else:
            Path(args[1]+'.txt').write_text('Skills\nPython, SQL\nExperience\nEngineer 2020 - 2024', encoding='utf-8')

    def test_scanned_page_and_mixed_page_preserve_native_first_review(self):
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name), patch.object(ocr, '_run_tool', side_effect=self.tool) as run:
            result = extract_resume(scanned_resume(mixed=True), 'pdf', 'synthetic-scan')
        self.assertEqual(result['status'], 'READY_FOR_REVIEW')
        self.assertEqual(result['contact']['email'], 'example@example.invalid')
        self.assertEqual(run.call_count, 2)  # Only the scanned page, not native page.
        self.assertEqual(run.call_args_list[0].args[1][1], '2')
        self.assertIn('Python', [fact['text'] for fact in result['facts']])
        self.assertTrue(all(not fact['verified'] and not fact['user_approved'] for fact in result['facts']))
        self.assertTrue(all(fact['confidence'] <= 70 for fact in result['facts']))
        self.assertTrue(any('OCR' in warning for warning in result['warnings']))
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_missing_engine_keeps_scanned_resume_explicitly_unprocessed(self):
        with patch.object(ocr.shutil, 'which', return_value=None):
            result = extract_resume(scanned_resume(), 'pdf', 'synthetic-scan')
        self.assertEqual(result['status'], 'NEEDS_OCR')
        self.assertEqual(result['facts'], [])
        self.assertTrue(any('unavailable' in warning for warning in result['warnings']))

    def test_unexpected_ocr_failure_cannot_discard_native_contact_text(self):
        with patch.object(ocr, 'ocr_pdf_pages', side_effect=RuntimeError('synthetic engine crash')):
            result = extract_resume(scanned_resume(mixed=True), 'pdf', 'synthetic-scan')
        self.assertEqual(result['status'], 'READY_FOR_REVIEW')
        self.assertEqual(result['contact']['email'], 'example@example.invalid')
        self.assertTrue(any('OCR' in warning for warning in result['warnings']))

    def test_timeout_preserves_native_text_and_cleans_temporary_files(self):
        def timeout(executable, args, limit):
            self.paths.append(Path(args[-1]).parent)
            raise subprocess.TimeoutExpired(executable, limit)
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name), patch.object(ocr, '_run_tool', side_effect=timeout):
            result = extract_resume(scanned_resume(mixed=True), 'pdf', 'synthetic-scan')
        self.assertEqual(result['status'], 'READY_FOR_REVIEW')
        self.assertEqual(result['contact']['email'], 'example@example.invalid')
        self.assertTrue(any('OCR' in warning for warning in result['warnings']))
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_page_limit_does_not_launch_unbounded_tools(self):
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name), patch.object(ocr, '_run_tool', side_effect=self.tool) as run:
            text, warnings = ocr.ocr_pdf_pages(b'synthetic', list(range(1, ocr.MAX_OCR_PAGES+3)))
        self.assertEqual(len(text), ocr.MAX_OCR_PAGES)
        self.assertEqual(run.call_count, ocr.MAX_OCR_PAGES*2)
        self.assertTrue(any('limit' in warning for warning in warnings))

    def test_process_wrapper_has_limits_no_shell_and_no_application_secrets(self):
        with patch.object(ocr.subprocess, 'run') as run, patch.dict(ocr.os.environ, {'DATABASE_URL':'private-database'}):
            ocr._run_tool('/usr/bin/tesseract', ['input.png', 'output'], 4)
        args, kwargs = run.call_args
        self.assertIn('RLIMIT_AS', args[0][2])
        self.assertIn('RLIMIT_FSIZE', args[0][2])
        self.assertIn('RLIMIT_CPU', args[0][2])
        self.assertIn('os.execv', args[0][2])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['timeout'], 4)
        self.assertNotIn('DATABASE_URL', kwargs['env'])
        self.assertEqual(kwargs['env']['OMP_THREAD_LIMIT'], '1')

    def test_oversized_raster_never_reaches_recognition(self):
        def render(executable, args, timeout):
            Image.new('RGB', (ocr.MAX_DIMENSION+1, 2)).save(args[-1]+'.png')
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name), patch.object(ocr, '_run_tool', side_effect=render) as run:
            text, warnings = ocr.ocr_pdf_pages(b'synthetic', [1])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(text, {})
        self.assertTrue(warnings)

    def test_total_deadline_and_busy_slot_skip_work_without_blocking(self):
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name), patch.object(ocr, '_run_tool') as run, patch.object(ocr.time, 'monotonic', side_effect=[0, ocr.TOTAL_TIMEOUT+1]):
            text, warnings = ocr.ocr_pdf_pages(b'synthetic', [1])
        run.assert_not_called()
        self.assertEqual(text, {})
        self.assertTrue(any('time limit' in warning for warning in warnings))
        with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), patch.object(ocr.shutil, 'which', side_effect=lambda name:'/fake/'+name):
            self.assertTrue(ocr._SLOT.acquire(blocking=False))
            try:
                text, warnings = ocr.ocr_pdf_pages(b'synthetic', [1])
                self.assertEqual(text, {})
                self.assertTrue(any('busy' in warning for warning in warnings))
            finally:
                ocr._SLOT.release()


if __name__ == '__main__':
    unittest.main()
