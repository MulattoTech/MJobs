"""Adversarial corroboration controls; connected admission is tested separately."""
from copy import deepcopy
import json
import unittest

import httpx

from services.company_discovery.domain_acquisition import corroborated_job_page


class NativeJobPageCorroborationTests(unittest.TestCase):
    def test_exact_reciprocal_job_identity_is_required(self):
        url = 'https://employer.example/careers/python?gh_jid=1234'
        proof = dict(website=url, native_job_id='1234', title='Python Engineer', tenant='example')
        node = {'@type': 'JobPosting', 'identifier': {'value': '1234'},
            'title': 'Python Engineer', 'url': url,
            'hiringOrganization': {'name': 'Example Employer', 'url': 'https://employer.example/'}}
        native = '<a href="https://job-boards.greenhouse.io/example/jobs/1234">Apply</a>'
        def response(value, links=native, final=url):
            return httpx.Response(200, text='<script type="application/ld+json">' +
                json.dumps(value) + '</script>' + links, request=httpx.Request('GET', final))
        self.assertTrue(corroborated_job_page(proof, 'Example Employer', response(node)))
        variants = []
        for key, value in [('identifier', {'value': '999'}), ('title', 'Different job'),
                           ('url', 'https://employer.example/different')]:
            variants.append((key, {**node, key: value}, native, url))
        for key, value in [('name', 'Different Employer'), ('url', 'https://directory.example/')]:
            other = deepcopy(node)
            other['hiringOrganization'][key] = value
            variants.append(('organization-' + key, other, native, url))
        variants.extend([
            ('missing-organization', {key: value for key, value in node.items() if key != 'hiringOrganization'}, native, url),
            ('ambiguous-jsonld', [node, node], native, url),
            ('cross-origin-final', node, native, 'https://another.example/careers/python'),
            ('no-reciprocal-link', node, '', url),
            ('board-only', node, '<a href="https://job-boards.greenhouse.io/example">Jobs</a>', url),
            ('wrong-requisition', node, native.replace('/1234', '/999'), url),
            ('wrong-tenant', node, native.replace('/example/', '/other/'), url),
            ('conflicting-tenants', node, native + native.replace('/example/', '/other/'), url),
            ('mixed-provider', node, native + '<a href="https://jobs.lever.co/other">Jobs</a>', url),
            ('lookalike-provider', node, native.replace('greenhouse.io/', 'greenhouse.io.evil.example/'), url),
            ('userinfo-provider', node, native.replace('job-boards.greenhouse.io', 'user@job-boards.greenhouse.io'), url),
        ])
        for name, value, links, final in variants:
            with self.subTest(name=name):
                self.assertFalse(corroborated_job_page(proof, 'Example Employer', response(value, links, final)))

