"""Direct non-fixture URL policy tests; no external requests are made."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from services.company_discovery.fetch_policy import _origin, bounded_get


class CompanyFetchPolicyTests(unittest.TestCase):
    def test_forbidden_schemes_credentials_private_and_multicast_literals(self):
        for url in ['file:///etc/passwd', 'ftp://8.8.8.8/a', 'https://user:pass@8.8.8.8/a',
                    'http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/',
                    'http://[::1]/', 'http://[fe80::1]/', 'http://[fc00::1]/',
                    'http://224.0.0.1/', 'http://[ff02::1]/', 'http://0.0.0.0/',
                    'http://localhost/', 'http://machine.internal/', 'http://8.8.8.8:8080/',
                    'http://8.8.8.8:443/', 'https://8.8.8.8:80/']:
            with self.subTest(url=url):
                self.assertIsNone(_origin(url))

    def test_only_public_dns_answers_and_default_ports_are_allowed(self):
        def records(*addresses):
            return [(2,1,6,'',(address,443)) for address in addresses]
        for answers in [records('10.0.0.1'), records('8.8.8.8','127.0.0.1'), [], records('224.0.0.1')]:
            with patch('services.company_discovery.fetch_policy.socket.getaddrinfo', return_value=answers):
                self.assertIsNone(_origin('https://employer.example/'))
        with patch('services.company_discovery.fetch_policy.socket.getaddrinfo', return_value=records('8.8.8.8')):
            self.assertIsNotNone(_origin('https://employer.example:443/'))
            self.assertIsNotNone(_origin('http://employer.example:80/'))
        with patch('services.company_discovery.fetch_policy.socket.getaddrinfo', side_effect=OSError('offline')):
            self.assertIsNone(_origin('https://employer.example/'))

    def test_invalid_expected_origin_cannot_disable_origin_restriction(self):
        class Client:
            bounded_redirects = True
            fixture_only = False
            def allowed(self, url): return True
            def get(self, url):
                raise AssertionError('Invalid expected origin must fail before transport')
        with self.assertRaisesRegex(ValueError, 'COMPANY_DISCOVERY_URL_BLOCKED'):
            bounded_get(Client(), 'https://8.8.8.8/jobs', 'http://127.0.0.1/')

    def test_redirect_result_must_retain_the_validated_origin(self):
        class Client:
            bounded_redirects = True
            fixture_only = False
            def allowed(self, url): return True
            def get(self, url): return SimpleNamespace(url='https://1.1.1.1/elsewhere')
        with self.assertRaisesRegex(ValueError, 'COMPANY_DISCOVERY_REDIRECT_BLOCKED'):
            bounded_get(Client(), 'https://8.8.8.8/jobs')


if __name__ == '__main__':
    unittest.main()
