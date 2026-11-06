"""Bounded complete Greenhouse metadata inventory; synthetic GET transport only."""
import copy
import unittest

import httpx

from services.discovery.sources.errors import SourceError
from services.discovery.sources.greenhouse import Greenhouse


def posting(identifier=1):
    return {"id": identifier, "internal_job_id": identifier + 1000,
            "title": f"Platform Engineer {identifier}", "location": {"name": "New York"},
            "absolute_url": f"https://employer.example/careers/{identifier}?gh_jid={identifier}"}


def inventory(count=2):
    return {"jobs": [posting(index + 1) for index in range(count)], "meta": {"total": count}}


class OversizeJSON(httpx.SyncByteStream):
    """Actually crosses the unchanged byte cap; never mocks the adapter exception."""
    closed = False

    def __iter__(self):
        yield b'{"jobs":[],"padding":"'
        for _ in range(78):
            yield b"x" * 65536
        yield b'"}'

    def close(self):
        self.closed = True


class GreenhouseMetadataInventoryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.streams = []

    def oversized(self):
        stream = OversizeJSON()
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    def adapter(self, metadata=None, *, initial=None, board=None, check=None):
        payload = inventory() if metadata is None else metadata

        def handle(request):
            self.requests.append(request)
            self.assertEqual((request.method, request.url.host), ("GET", "boards-api.greenhouse.io"))
            if request.url.path == "/v1/boards/example":
                return board() if board else httpx.Response(200, json={"name": "Example"})
            self.assertEqual(request.url.path, "/v1/boards/example/jobs")
            if request.url.params.get("content") == "true":
                return initial() if initial else self.oversized()
            self.assertEqual(str(request.url.query, "ascii"), "")
            return payload() if callable(payload) else httpx.Response(200, json=payload)

        return Greenhouse("example", transport=httpx.MockTransport(handle), check=check)

    def test_payload_limit_falls_back_to_all_765_exact_metadata_rows(self):
        payload = inventory(765)
        adapter = self.adapter(payload)
        batch = adapter.search()
        self.assertTrue(batch.complete_inventory)
        self.assertEqual(batch.endpoint, "https://boards-api.greenhouse.io/v1/boards/example/jobs")
        self.assertEqual([item.raw for item in batch.items], payload["jobs"])
        self.assertEqual({item.company.identity_parts for item in batch.items}, {("greenhouse", "example")})
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assertEqual(adapter.health(), "HEALTHY")
        for item in (batch.items[0], batch.items[-1]):
            parsed = adapter.normalize(item.raw)
            self.assertEqual(parsed["requisition"], str(item.raw["id"]))
            self.assertEqual(parsed["description"], "")
            self.assertEqual(parsed["attributes"]["description_status"], "UNAVAILABLE")
            self.assertIsNone(parsed["attributes"]["salary_min"])
            self.assertEqual(parsed["skills"], [])
            self.assertNotIn("content", item.raw)

    def test_normal_content_inventory_does_not_request_metadata(self):
        payload = inventory()
        payload["jobs"][0]["content"] = "<p>Build services</p>"
        adapter = self.adapter(initial=lambda: httpx.Response(200, json=payload))
        batch = adapter.search()
        self.assertEqual([item.raw for item in batch.items], payload["jobs"])
        self.assertEqual(adapter.normalize(batch.items[0].raw)["description"], "Build services")
        self.assertEqual(len(self.requests), 2)

    def test_other_initial_failures_never_trigger_metadata(self):
        def network():
            raise httpx.ConnectError("synthetic connection failure")

        cases = [
            (lambda: httpx.Response(429), "SOURCE_RATE_LIMITED"),
            (lambda: httpx.Response(503), "SOURCE_HTTP_503"),
            (lambda: httpx.Response(302), "SOURCE_REDIRECT_BLOCKED"),
            (lambda: httpx.Response(200, text="not json"), "SOURCE_INVALID_CONTENT_TYPE"),
            (lambda: httpx.Response(200, content=b"{", headers={"content-type": "application/json"}), "SOURCE_INVALID_JSON"),
            (network, "synthetic connection failure"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                self.requests.clear()
                with self.assertRaisesRegex((SourceError, httpx.HTTPError), code):
                    self.adapter(initial=response).search()
                self.assertEqual(len(self.requests), 2)

    def test_board_failure_or_invalid_board_never_triggers_metadata(self):
        for response in (lambda: httpx.Response(429), self.oversized, lambda: httpx.Response(200, json=[])):
            with self.subTest(response=response):
                self.requests.clear()
                with self.assertRaises(SourceError):
                    self.adapter(board=response).search()
                self.assertEqual(len(self.requests), 2)

    def test_incomplete_or_malformed_metadata_never_becomes_complete_inventory(self):
        cases = [[], {}, {"jobs": {}}, {"jobs": []},
                 {"jobs": [], "meta": {"total": True}},
                 {"jobs": [], "meta": {"total": "0"}},
                 {"jobs": [], "meta": {"total": 1}},
                 {"jobs": [posting(), posting()], "meta": {"total": 2}},
                 inventory(1001)]
        for field, value in (("id", True), ("id", "1"), ("id", 0), ("id", -1),
                             ("title", ""), ("location", ["New York"]),
                             ("absolute_url", ""), ("absolute_url", "http://employer.example/job"),
                             ("absolute_url", "https://user:secret@employer.example/job"),
                             ("absolute_url", "https://job-boards.greenhouse.io/other/jobs/1"),
                             ("absolute_url", "https://job-boards.greenhouse.io/example/jobs/99")):
            row = posting()
            row[field] = value
            cases.append({"jobs": [row], "meta": {"total": 1}})
        for payload in cases:
            with self.subTest(payload=str(payload)[:150]):
                self.requests.clear()
                with self.assertRaises(SourceError):
                    self.adapter(payload).search()
                self.assertEqual(len(self.requests), 3)

    def test_metadata_transport_failure_or_oversize_is_terminal_without_another_fallback(self):
        def network():
            raise httpx.ReadTimeout("synthetic read timeout")

        for response, code in ((self.oversized, "SOURCE_PAYLOAD_LIMIT"),
                               (lambda: httpx.Response(429), "SOURCE_RATE_LIMITED"),
                               (lambda: httpx.Response(503), "SOURCE_HTTP_503"),
                               (network, "synthetic read timeout")):
            with self.subTest(code=code):
                self.requests.clear()
                with self.assertRaisesRegex((SourceError, httpx.HTTPError), code):
                    self.adapter(response).search()
                self.assertEqual(len(self.requests), 3)

    def test_cancellation_after_large_response_prevents_metadata_request(self):
        def check():
            if any(stream.closed for stream in self.streams):
                raise RuntimeError("synthetic cancellation")

        with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
            self.adapter(check=check).search()
        self.assertEqual(len(self.requests), 2)

    def test_empty_metadata_inventory_requires_explicit_zero_total(self):
        batch = self.adapter(inventory(0)).search()
        self.assertEqual(batch.items, ())
        self.assertTrue(batch.complete_inventory)

    def test_on_demand_detail_stays_a_separate_bounded_exact_id_get(self):
        raw = posting()
        raw["content"] = "<p>Actual provider detail</p>"
        def detail(request):
            self.requests.append(request)
            self.assertEqual(request.url.path, "/v1/boards/example/jobs/1")
            self.assertEqual(dict(request.url.params), {"pay_transparency": "true"})
            return httpx.Response(200, json=copy.deepcopy(raw))
        adapter = Greenhouse("example", transport=httpx.MockTransport(detail))
        self.assertEqual(adapter.fetch(1), raw)
        self.assertEqual(len(self.requests), 1)
