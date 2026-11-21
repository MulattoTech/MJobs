"""JobPosting sameAs is employer-origin evidence only within reciprocal proof."""
from copy import deepcopy
import json
import unittest

import httpx

from services.company_discovery.domain_acquisition import corroborated_job_page


class OfficialDomainSameAsTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://employer.example/careers/python?gh_jid=1234'
        self.proof = dict(website=self.url, native_job_id='1234', title='Python Engineer', tenant='example')
        # Google's JobPosting guidance uses a scalar hiringOrganization.sameAs:
        # https://developers.google.com/search/docs/appearance/structured-data/job-posting
        self.node = {'@type': 'JobPosting', 'identifier': {'value': '1234'},
            'title': 'Python Engineer', 'url': self.url,
            'hiringOrganization': {'name': 'Example Employer', 'sameAs': 'https://employer.example/'}}
        self.native = '<a href="https://job-boards.greenhouse.io/example/jobs/1234">Apply</a>'

    def corroborated(self, node, *, links=None, final=None):
        response = httpx.Response(200, text='<script type="application/ld+json">' +
            json.dumps(node) + '</script>' + (self.native if links is None else links),
            request=httpx.Request('GET', final or self.url))
        return corroborated_job_page(self.proof, 'Example Employer', response)

    def test_sameas_identifies_same_employer_origin_when_url_is_absent(self):
        self.assertTrue(self.corroborated(self.node))

    def test_sameas_cannot_rescue_an_explicit_invalid_or_foreign_url(self):
        for value in (None, '', 'https://directory.example/', 'http://employer.example/'):
            with self.subTest(url=value):
                node = deepcopy(self.node)
                node['hiringOrganization']['url'] = value
                self.assertFalse(self.corroborated(node))

    def test_existing_url_does_not_require_sameas_to_be_an_employer_website(self):
        # sameAs may identify a social profile; the established url path stays valid.
        node = deepcopy(self.node)
        node['hiringOrganization'].update(url='https://employer.example/',
            sameAs='https://social.example/employer')
        self.assertTrue(self.corroborated(node))

    def test_fallback_requires_one_scalar_same_origin_https_url(self):
        invalid = (None, '', 123, True, {}, {'url': 'https://employer.example/'},
            ['https://employer.example/'],
            'http://employer.example/', 'https://directory.example/',
            'https://employer.example.evil.example/', 'https://user@employer.example/',
            'https://employer.example:444/', '/careers', 'https://[invalid/')
        for value in invalid:
            with self.subTest(same_as=value):
                node = deepcopy(self.node)
                node['hiringOrganization']['sameAs'] = value
                self.assertFalse(self.corroborated(node))

    def test_sameas_keeps_exact_job_and_company_identity_requirements(self):
        variants = []
        for key, value in [('identifier', {'value': '999'}), ('title', 'Different job'),
                           ('url', 'https://employer.example/different'),
                           ('hiringOrganization', {'name': 'Different Employer',
                                                  'sameAs': 'https://employer.example/'}),
                           ('hiringOrganization', None)]:
            variants.append((key, {**self.node, key: value}))
        variants.append(('ambiguous-postings', [self.node, self.node]))
        for name, value in variants:
            with self.subTest(variant=name):
                self.assertFalse(self.corroborated(value))
        self.assertFalse(self.corroborated(self.node, final='https://another.example/careers/python'))

    def test_sameas_keeps_exact_native_reciprocal_link_requirements(self):
        variants = {
            'missing': '',
            'board-only': '<a href="https://job-boards.greenhouse.io/example">Jobs</a>',
            'wrong-job': self.native.replace('/1234', '/999'),
            'wrong-tenant': self.native.replace('/example/', '/other/'),
            'conflicting-tenant': self.native + self.native.replace('/example/', '/other/'),
            'mixed-provider': self.native + '<a href="https://jobs.lever.co/other">Jobs</a>',
            'lookalike': self.native.replace('greenhouse.io/', 'greenhouse.io.evil.example/'),
            'userinfo': self.native.replace('job-boards.greenhouse.io', 'user@job-boards.greenhouse.io'),
            'query': self.native.replace('/1234"', '/1234?job=999"'),
            'fragment': self.native.replace('/1234"', '/1234#other"'),
        }
        for name, links in variants.items():
            with self.subTest(variant=name):
                self.assertFalse(self.corroborated(self.node, links=links))
