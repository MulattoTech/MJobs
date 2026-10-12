"""Public Lever inventories keep region, pagination and listing identity intact."""
import unittest
import threading
import time
from unittest.mock import patch

import httpx

from services.ats_detection import board_links
from services.ats_detection.detector import detect_ats
from services.discovery.sources.registry import source_adapter
from services.discovery.sources.errors import SourceError


def posting(number=1, *, host='jobs.lever.co', site='example'):
    identifier = f'00000000-0000-0000-0000-{number:012d}'
    return {'id': identifier, 'text': 'Senior Python Engineer',
            'description': '<p>Python remote role. Full-time.</p>',
            'categories': {'location': 'Remote, United States', 'commitment': 'Full-time'},
            'workplaceType': 'remote', 'hostedUrl': f'https://{host}/{site}/{identifier}',
            'applyUrl': f'https://{host}/{site}/{identifier}/apply'}


class LeverSourceTests(unittest.TestCase):
    def test_inventory_deadline_interrupts_an_inflight_stream_and_restores_parent_check(self):
        closed = threading.Event()
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                closed.wait(2)
                yield b'[]'
            def close(self):
                closed.set()
        adapter = self.adapter(handler=lambda request: httpx.Response(
            200, headers={'content-type':'application/json'}, stream=SlowStream()))
        parent = adapter.check
        started = time.monotonic()
        with patch('services.discovery.sources.lever.MAX_INVENTORY_SECONDS', 0.15):
            with self.assertRaisesRegex(SourceError, 'SOURCE_TIME_LIMIT'):
                adapter.search()
        self.assertLess(time.monotonic()-started, 1)
        self.assertTrue(closed.is_set())
        self.assertIs(adapter.check, parent)

    def test_malformed_other_provider_does_not_hide_valid_lever_link(self):
        html = ('<iframe src="https://boards.greenhouse.io/embed/job_board?for=a&for=b"></iframe>'
                '<a href="https://jobs.eu.lever.co/example">Jobs</a>')
        self.assertEqual(detect_ats('https://employer.example/careers',html).provider,'lever')
        malformed = '<a href="https://jobs.lever.co/:invalid">Jobs</a>'
        self.assertIsNone(detect_ats('https://employer.example/careers',malformed).provider)

    def adapter(self, tenant='global:example', handler=None):
        return source_adapter({'provider': 'lever', 'tenant': tenant},
            transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, json=[]))))

    def test_supported_inventory_uses_exact_regional_endpoint_and_normalization(self):
        for region, api, host in [('global', 'api.lever.co', 'jobs.lever.co'),
                                   ('eu', 'api.eu.lever.co', 'jobs.eu.lever.co')]:
            with self.subTest(region=region):
                requests = []
                def serve(request):
                    requests.append(request)
                    return httpx.Response(200, json=[posting(host=host)])
                adapter = self.adapter(region+':example', serve)
                batch = adapter.search()
                self.assertTrue(batch.complete_inventory)
                self.assertEqual(batch.endpoint, f'https://{api}/v0/postings/example')
                self.assertEqual([r.method for r in requests], ['GET'])
                self.assertEqual(requests[0].url.host, api)
                self.assertEqual(dict(requests[0].url.params), {'mode':'json','skip':'0','limit':'100'})
                job = adapter.normalize(batch.items[0].raw)
                self.assertEqual(job['requisition'], posting()['id'])
                self.assertEqual(job['attributes']['provider'], 'lever')
                self.assertEqual(job['attributes']['tenant'], region+':example')
                self.assertEqual(job['application_url'], posting(host=host)['hostedUrl'])
                self.assertEqual(job['remote_type'], 'REMOTE')
                self.assertIn('Python', job['skills'])
                self.assertFalse(adapter.capabilities().as_dict()['direct_application'])

    def test_full_pages_are_exhausted_before_complete_inventory(self):
        offsets = []
        def serve(request):
            offset = int(request.url.params['skip']); offsets.append(offset)
            count = 100 if offset < 200 else 1
            return httpx.Response(200, json=[posting(offset+i+1) for i in range(count)])
        batch = self.adapter(handler=serve).search()
        self.assertEqual(offsets, [0,100,200])
        self.assertEqual(len(batch.items), 201)
        self.assertTrue(batch.complete_inventory)

    def test_later_page_failure_cannot_return_partial_inventory(self):
        def serve(request):
            return (httpx.Response(200, json=[posting(i+1) for i in range(100)])
                    if request.url.params['skip']=='0' else httpx.Response(503))
        with self.assertRaises(SourceError):
            self.adapter(handler=serve).search()

    def test_page_cap_and_duplicate_inventory_fail_closed(self):
        def serve(request):
            offset = int(request.url.params['skip'])
            return httpx.Response(200, json=[posting(offset+i+1) for i in range(100)])
        with self.assertRaises(SourceError):
            self.adapter(handler=serve).search()
        with self.assertRaises(SourceError):
            self.adapter(handler=lambda request: httpx.Response(200, json=[posting(),posting()])).search()

    def test_cross_region_site_malformed_ids_and_urls_are_rejected(self):
        invalid = [posting(host='jobs.eu.lever.co'), posting(site='other'),
                   dict(posting(), id='../escape'), dict(posting(), hostedUrl='https://evil.invalid/job'),
                   dict(posting(), applyUrl=posting(host='jobs.eu.lever.co')['applyUrl'])]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(SourceError):
                self.adapter(handler=lambda request: httpx.Response(200, json=[row])).search()

    def test_structural_board_links_preserve_region_and_reject_ambiguity(self):
        parser = getattr(board_links, 'lever_board_tenants', None)
        self.assertTrue(callable(parser), 'Lever enrollment needs structural board evidence')
        for region, host in [('global','jobs.lever.co'), ('eu','jobs.eu.lever.co')]:
            html = f'<a href="https://{host}/example">Open jobs</a>'
            self.assertEqual(parser('https://employer.example/careers', html), {region+':example'})
            self.assertEqual(detect_ats('https://employer.example/careers',html).provider, 'lever')
        self.assertEqual(parser('https://employer.example/',
            '<a href="https://jobs.lever.co.attacker.invalid/example">jobs</a>'), set())
        self.assertEqual(parser('https://employer.example/',
            '<p>https://jobs.lever.co/example</p>'), set())
        self.assertEqual(len(parser('https://employer.example/',
            '<a href="https://jobs.lever.co/example">jobs</a><iframe src="https://jobs.eu.lever.co/example"></iframe>')),2)
