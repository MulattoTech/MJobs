"""Table-driven, synthetic contract checks for globally registered source adapters."""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

import httpx

from services.discovery.sources.errors import SourceError
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.registry import source_adapter
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


PRIVATE_SENTINEL = "SOURCE-CONTRACT-PRIVATE-SENTINEL"
LEVER_ID = "12345678-1234-1234-1234-123456789abc"


FIXTURES = {
    "greenhouse": {
        "source": {"provider": "greenhouse", "tenant": "example"},
        "raw": {
            "id": 1234,
            "title": "Python Engineer",
            "location": {"name": "Remote - United States"},
            "content": "<p>Build services with Python, SQL, and APIs.</p>",
            "first_published_at": "2026-09-29T12:00:00Z",
            "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1234",
        },
        "expected_url": "https://job-boards.greenhouse.io/example/jobs/1234",
        "id_key": "id",
        "title_key": "title",
        "description_key": "content",
        "url_key": "absolute_url",
    },
    "lever": {
        "source": {"provider": "lever", "tenant": f"global:example"},
        "raw": {
            "id": LEVER_ID,
            "text": "Python Engineer",
            "hostedUrl": f"https://jobs.lever.co/example/{LEVER_ID}",
            "applyUrl": f"https://jobs.lever.co/example/{LEVER_ID}",
            "descriptionPlain": "Build services with Python, SQL, and APIs.",
            "categories": {"location": "Remote - United States"},
            "createdAt": 1790683200000,
        },
        "expected_url": f"https://jobs.lever.co/example/{LEVER_ID}",
        "id_key": "id",
        "title_key": "text",
        "description_key": "descriptionPlain",
        "url_key": "hostedUrl",
    },
    "remotive": {
        "source": {"provider": "remotive", "tenant": "contract-fixture"},
        "raw": {
            "id": 1001,
            "title": "Python Engineer",
            "company_name": "Example Systems",
            "description": "<p>Build services with Python, SQL, and APIs.</p>",
            "url": "https://remotive.com/remote-jobs/software-dev/python-engineer-1001",
            "publication_date": "2026-09-29T12:00:00+00:00",
            "candidate_required_location": "Worldwide",
        },
        "expected_url": "https://remotive.com/remote-jobs/software-dev/python-engineer-1001",
        "id_key": "id",
        "title_key": "title",
        "description_key": "description",
        "url_key": "url",
    },
    "remoteok": {
        "source": {"provider": "remoteok", "tenant": "contract-fixture"},
        "raw": {
            "id": "2001",
            "position": "Python Engineer",
            "company": "Example Systems",
            "description": "<p>Build services with Python, SQL, and APIs.</p>",
            "url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
            "apply_url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
            "date": "2026-09-29T12:00:00+00:00",
            "location": "Worldwide",
        },
        "expected_url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
        "id_key": "id",
        "title_key": "position",
        "description_key": "description",
        "url_key": "url",
    },
    "remote_ok": {
        "source": {"provider": "remote_ok", "tenant": "contract-fixture"},
        "raw": {
            "id": "2001",
            "position": "Python Engineer",
            "company": "Example Systems",
            "description": "<p>Build services with Python, SQL, and APIs.</p>",
            "url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
            "apply_url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
            "date": "2026-09-29T12:00:00+00:00",
            "location": "Worldwide",
        },
        "expected_url": "https://remoteok.com/remote-jobs/python-engineer-example-2001",
        "id_key": "id",
        "title_key": "position",
        "description_key": "description",
        "url_key": "url",
    },
    "wwr": {
        "source": {"provider": "wwr", "tenant": "contract-fixture"},
        "rss": b"""<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel><title>Fixture jobs</title>
            <item><title>Example Systems: Python Engineer</title>
            <region>Worldwide</region><type>Full-Time</type>
            <description>&lt;p&gt;Build services with Python, SQL, and APIs.&lt;/p&gt;</description>
            <pubDate>Tue, 29 Sep 2026 12:00:00 +0000</pubDate>
            <guid>https://weworkremotely.com/remote-jobs/example-python-engineer</guid>
            <link>https://weworkremotely.com/remote-jobs/example-python-engineer</link>
            </item></channel></rss>""",
        "expected_url": "https://weworkremotely.com/remote-jobs/example-python-engineer",
        "id_key": "guid",
        "title_key": "title",
        "description_key": "description",
        "url_key": "link",
    },
}


def _registry_provider_literals() -> set[str]:
    """Read the provider selectors in the registry so additions require coverage."""
    tree = ast.parse(inspect.getsource(source_adapter))
    providers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
            continue
        left = node.left
        if not (
            isinstance(left, ast.Subscript)
            and isinstance(left.value, ast.Name)
            and left.value.id == "source"
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == "provider"
        ):
            continue
        value = node.comparators[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            providers.add(value.value)
        elif isinstance(value, (ast.Tuple, ast.List)):
            providers.update(
                item.value for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return providers


def _fixture_response(provider: str, request: httpx.Request, raw_override=None) -> httpx.Response:
    raw = FIXTURES[provider].get("raw") if raw_override is None else raw_override
    if provider == "greenhouse":
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [raw]})
        return httpx.Response(200, json={"name": "Example Systems"})
    if provider == "lever":
        return httpx.Response(200, json=[raw])
    if provider == "remotive":
        return httpx.Response(200, json={"jobs": [raw]})
    if provider in {"remoteok", "remote_ok"}:
        return httpx.Response(200, json=[raw])
    if provider == "wwr":
        return httpx.Response(200, content=FIXTURES[provider]["rss"] if raw_override is None else raw,
                              headers={"content-type": "application/rss+xml"})
    raise AssertionError(f"uncovered test fixture for provider {provider}")


class SourceAdapterContractTests(unittest.TestCase):
    def test_fixture_table_covers_exact_registry_and_searches_without_network(self):
        self.assertEqual(set(FIXTURES), _registry_provider_literals())

        for provider, fixture in FIXTURES.items():
            with self.subTest(provider=provider):
                requests = []

                def respond(request, selected=provider):
                    requests.append(request)
                    return _fixture_response(selected, request)

                adapter = source_adapter(
                    fixture["source"],
                    transport=httpx.MockTransport(respond),
                )
                batch = adapter.search()
                self.assertGreaterEqual(len(batch.items), 1)
                self.assertTrue(batch.endpoint.startswith("https://"))
                self.assertTrue(requests)
                self.assertTrue(all(request.method == "GET" for request in requests))
                self.assertTrue(all(request.url.scheme == "https" for request in requests))

    def test_each_registered_adapter_emits_repeatable_canonical_job_contract(self):
        for provider, fixture in FIXTURES.items():
            with self.subTest(provider=provider):
                adapter = source_adapter(fixture["source"], transport=httpx.MockTransport(
                    lambda request, selected=provider: _fixture_response(selected, request)
                ))
                batch = adapter.search()
                raw = batch.items[0].raw
                normalized = adapter.normalize(raw)
                repeated = adapter.normalize(deepcopy(raw))

                self.assertEqual(normalized, repeated)
                self.assertTrue(str(normalized["requisition"]).strip())
                attributes = normalized["attributes"]
                self.assertIn("native_posting_id", attributes)
                self.assertIsNotNone(attributes["native_posting_id"])
                self.assertEqual(normalized["application_url"], fixture["expected_url"])
                parsed_url = urlsplit(normalized["application_url"])
                self.assertIn(parsed_url.scheme, {"http", "https"})
                self.assertIsNotNone(parsed_url.hostname)
                self.assertIsNone(parsed_url.username)
                self.assertIsNone(parsed_url.password)
                self.assertFalse(parsed_url.fragment)
                self.assertTrue(normalized["description"].strip())
                self.assertNotIn("<p>", normalized["description"])
                self.assertIn("reported_posted_at", attributes)
                reported = attributes["reported_posted_at"]
                if reported is not None:
                    parsed_date = datetime.fromisoformat(reported.replace("Z", "+00:00"))
                    self.assertIsNotNone(parsed_date.utcoffset())

    def test_direct_normalizers_keep_sparse_description_as_explicit_empty(self):
        for provider, fixture in FIXTURES.items():
            with self.subTest(provider=provider):
                adapter = source_adapter(fixture["source"], transport=httpx.MockTransport(
                    lambda request, selected=provider: _fixture_response(selected, request)
                ))
                raw = deepcopy(adapter.search().items[0].raw)
                raw[fixture["description_key"]] = ""
                self.assertEqual(adapter.normalize(raw)["description"], "")

    def test_collection_rejects_jobs_without_nonblank_descriptions(self):
        """The common ingestion boundary must not persist empty descriptions."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(f"sqlite:///{(Path(temporary_directory) / 'contract.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(PROFILE)
            try:
                for provider in ("greenhouse", "lever", "remotive", "remoteok", "wwr"):
                    fixture = FIXTURES[provider]
                    source_id = configure_source(
                        store,
                        fixture["source"]["tenant"],
                        provider=provider,
                    )
                    if provider == "wwr":
                        empty_description = fixture["rss"].replace(
                            b"Build services with Python, SQL, and APIs.", b""
                        )
                    else:
                        empty_description = deepcopy(fixture["raw"])
                        empty_description[fixture["description_key"]] = ""

                    def respond(request, selected=provider, raw=empty_description):
                        return _fixture_response(selected, request, raw_override=raw)

                    with self.subTest(provider=provider), self.assertRaisesRegex(
                        ValueError, "SOURCE_NO_VALID_JOBS"
                    ):
                        collect_source(
                            store,
                            source_id,
                            "fixture",
                            transport=httpx.MockTransport(respond),
                            refresh_matches=False,
                        )
            finally:
                store.engine.dispose()

    def test_missing_malformed_fields_fail_as_source_errors_without_echoing_input(self):
        for provider, fixture in FIXTURES.items():
            with self.subTest(provider=provider):
                adapter = source_adapter(fixture["source"], transport=httpx.MockTransport(
                    lambda request, selected=provider: _fixture_response(selected, request)
                ))
                raw_fixture = deepcopy(adapter.search().items[0].raw)
                # WWR has alternate guid/link identity; remove both so the
                # fixture cannot accidentally pass through its fallback.
                missing_id = deepcopy(raw_fixture)
                missing_id.pop(fixture["id_key"], None)
                if provider == "wwr":
                    missing_id.pop("link", None)
                invalid_values = (
                    ("missing_id", missing_id),
                    ("missing_title", {**raw_fixture, fixture["title_key"]: ""}),
                    ("private_extra_field", {**raw_fixture, "extra_private": PRIVATE_SENTINEL}),
                )
                for case_name, raw in invalid_values:
                    with self.subTest(case=case_name):
                        try:
                            normalized = adapter.normalize(raw)
                        except SourceError as failure:
                            self.assertNotIn(PRIVATE_SENTINEL, str(failure))
                            self.assertNotIn(PRIVATE_SENTINEL, repr(failure))
                        except Exception as failure:  # source errors must be stable, not crashes
                            self.fail(f"{provider}/{case_name} raised {type(failure).__name__}")
                        else:
                            if case_name == "private_extra_field":
                                self.assertNotIn(PRIVATE_SENTINEL, json.dumps(normalized, sort_keys=True))
                            else:
                                self.fail(f"{provider}/{case_name} was accepted")


if __name__ == "__main__":
    unittest.main()
