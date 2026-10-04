import unittest
from services.documents.validation import pdf_tokens_match


class PdfUrlFidelityTests(unittest.TestCase):
    def test_wraps_are_limited_to_identical_url_characters(self):
        expected='Portfolio: https://example.test/long-path?a=1&b=2 Python'
        self.assertTrue(pdf_tokens_match('Portfolio: https://example.test/long-\npath?a=1&b=2 Python',expected))
        self.assertTrue(pdf_tokens_match('HTTPS://example.test/long-\npath','HTTPS://example.test/long-path'))
        for altered in ('Portfolio: https://example.test/long-path?a=1&b=3 Python',
                        'Portfolio: https://example.test/long-path?a=1&b=2 Py thon',
                        'Portfolio: https://example.test/long-path?a=1&b=2 Python expert',
                        'Portfolio: https://example.test/long-path?a=1&b=2',
                        'Portfolio: https://evil.test/long-path?a=1&b=2 Python'):
            self.assertFalse(pdf_tokens_match(altered,expected),altered)
