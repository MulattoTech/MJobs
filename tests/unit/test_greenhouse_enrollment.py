"""Only exact, unambiguous official-page board links authorize tenant selection."""
import unittest

from services.company_discovery.ats_enrollment import greenhouse_tenant
from services.company_discovery.public_client import PublicFetchError


class GreenhouseEnrollmentTests(unittest.TestCase):
    def test_supported_links_and_embeds_select_the_same_tenant(self):
        for url in ('https://boards.greenhouse.io/Example',
                    'https://job-boards.greenhouse.io/example/jobs/123',
                    'https://boards.greenhouse.io/embed/job_board/js?for=example'):
            with self.subTest(url=url):
                self.assertEqual(greenhouse_tenant('https://employer.example/careers',
                    '<iframe src="' + url + '"></iframe>'), 'example')

    def test_untrusted_hosts_paths_schemes_and_text_cannot_select_a_board(self):
        for markup in (
            '<script>const board="https://boards.greenhouse.io/example";</script>',
            '<a href="https://boards.greenhouse.io.attacker.example/example">Jobs</a>',
            '<a href="https://user@boards.greenhouse.io/example">Jobs</a>',
            '<a href="http://boards.greenhouse.io/example">Jobs</a>',
            '<a href="https://boards.greenhouse.io:444/example">Jobs</a>',
            '<a href="https://boards.greenhouse.io/example%2f..%2fforeign">Jobs</a>',
            '<iframe src="https://boards.greenhouse.io/embed/job_board/js?for=example&amp;for=other"></iframe>',
            '<a href="https://job-boards.greenhouse.io/example"></a><a href="https://boards.greenhouse.io/other"></a>',
        ):
            with self.subTest(markup=markup), self.assertRaises(PublicFetchError):
                greenhouse_tenant('https://employer.example/careers', markup)
