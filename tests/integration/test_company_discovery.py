"""Dedicated company-resolution, trigger, and flywheel acceptance."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, text

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.resolution import resolve_company
from services.company_discovery.triggers import queue_company_discovery
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class CompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_resolver_returns_identity_confidence_and_evidence(self):
        candidate = {
            "company_name": "Example Systems Inc.",
            "company_website": "https://example.test/company",
            "application_url": "https://careers.example.test/jobs/123",
        }
        first = resolve_company(self.store, candidate)
        self.assertTrue(first["is_new"])
        self.assertEqual(first["company_id"], identity(candidate["company_website"]))
        self.assertEqual(first["confidence"], 0.45)
        self.assertEqual(first["evidence"], ["CANDIDATE_DOMAIN_UNVERIFIED"])
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": first["company_id"],
                "name": "Example Systems Inc.",
                "domain": candidate["company_website"],
            })
        second = resolve_company(self.store, candidate)
        self.assertFalse(second["is_new"])
        self.assertEqual(second["company_id"], first["company_id"])
        self.assertIn("NORMALIZED_NAME_EXACT", second["evidence"])
        self.assertNotIn("OFFICIAL_DOMAIN_EXACT", second["evidence"])

    def test_aggregator_domain_does_not_establish_company_identity(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "known-company", "name": "Known Corporation",
                "domain": "https://linkedin.com/company/known",
            })
        result = resolve_company(self.store, {
            "company_name": "Different Employer",
            "company_website": "https://linkedin.com/company/different",
        })
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["company_id"], "known-company")
        self.assertEqual(result["confidence"], 0.45)
        self.assertEqual(result["evidence"], ["UNTRUSTED_AGGREGATOR_DOMAIN"])

    def test_resolver_matches_only_a_persisted_verified_company_domain(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "verified-company", "name": "Acme Holdings", "domain": "",
            })
            try:
                connection.execute(text("""
                    INSERT INTO company_domains
                        (id, company_id, domain, confidence, verified, verification_method,
                         first_seen_at, last_verified_at, evidence)
                    VALUES
                        ('verified-domain', 'verified-company', 'acme.example', 0.99, 1,
                         'FIRST_PARTY_CAREER_LINK', '2026-10-01T00:00:00Z',
                         '2026-10-01T00:00:00Z', '[]')
                """))
            except Exception as failure:
                self.fail(f"verified company domain should be persistable: {failure}")
        result = resolve_company(self.store, {
            "company_name": "Acme Holdings",
            "application_url": "https://careers.acme.example/jobs",
        })
        self.assertFalse(result["is_new"])
        self.assertEqual(result["company_id"], "verified-company")
        self.assertIn("VERIFIED_DOMAIN_MATCH", result["evidence"])

    def test_resolver_uses_a_verified_company_alias(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "alias-company", "name": "Acme Holdings", "domain": "",
            })
            try:
                connection.execute(text("""
                    INSERT INTO company_aliases
                        (id, company_id, alias, normalized_alias, alias_type, confidence,
                         verified, source, evidence, created_at)
                    VALUES
                        ('alias-acme', 'alias-company', 'Acme Data Labs', 'acme data labs',
                         'BRAND', 0.93, 1, 'OFFICIAL_CAREER_PAGE',
                         '["verified company page"]', '2026-10-01T00:00:00Z')
                """))
            except Exception as failure:
                self.fail(f"verified company alias should be persistable: {failure}")
        result = resolve_company(self.store, {"company_name": "Acme Data Labs"})
        self.assertFalse(result["is_new"])
        self.assertEqual(result["company_id"], "alias-company")
        self.assertEqual(result["confidence"], 0.93)
        self.assertIn("VERIFIED_ALIAS_EXACT", result["evidence"])

    def test_domain_evidence_promotes_candidate_without_later_downgrade(self):
        from services.company_discovery.domains import record_company_domain, resolve_official_domain

        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "promoted-company", "name": "Promoted Co", "domain": "",
            })
        candidate = record_company_domain(self.store, "promoted-company", "https://www.promoted.example/jobs")
        self.assertFalse(candidate["verified"])
        verified = record_company_domain(
            self.store, "promoted-company", "promoted.example", "FIRST_PARTY_JOB_LINK", ["first-party URL"]
        )
        self.assertTrue(verified["verified"])
        retained = record_company_domain(self.store, "promoted-company", "promoted.example")
        self.assertTrue(retained["verified"])
        self.assertEqual(retained["last_verified_at"], verified["last_verified_at"])
        winner = resolve_official_domain([
            {"domain": "linkedin.com/company/promoted", "method": "FIRST_PARTY_JOB_LINK"},
            {"domain": "promoted.example", "method": "FIRST_PARTY_JOB_LINK", "evidence": ["job link"]},
            {"domain": "search.example", "method": "CONFIGURED_WEB_SEARCH"},
        ])
        self.assertEqual(winner["status"], "VERIFIED")
        self.assertEqual(winner["domain"], "promoted.example")

    def test_domain_resolver_preserves_equal_rank_ambiguity(self):
        from services.company_discovery.domains import resolve_official_domain

        result = resolve_official_domain([
            {"domain": "one.example", "method": "ATS_COMPANY_METADATA"},
            {"domain": "two.example", "method": "ATS_COMPANY_METADATA"},
        ])
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["candidates"], ["one.example", "two.example"])

    def test_company_domain_normalization_rejects_private_ip_literals(self):
        from services.company_discovery.domains import normalize_domain

        for value in ("http://127.0.0.1/admin", "http://10.0.0.8", "http://169.254.169.254/latest"):
            with self.subTest(value=value):
                self.assertEqual(normalize_domain(value), "")

    def test_alias_evidence_does_not_downgrade_verified_alias(self):
        from services.company_discovery.aliases import record_company_alias

        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "alias-retained", "name": "Canonical Co", "domain": "",
            })
        verified = record_company_alias(
            self.store, "alias-retained", "C.C.", alias_type="ABBREVIATION", verified=True,
            source="OFFICIAL_CAREER_PAGE", evidence=["official page"], confidence=0.93,
        )
        retained = record_company_alias(self.store, "alias-retained", "C.C.", verified=False)
        self.assertTrue(retained["verified"])
        self.assertEqual(retained["confidence"], verified["confidence"])

    def test_trigger_is_durable_and_idempotent(self):
        candidate = {
            "company_name": "Example Systems Inc.",
            "company_website": "http://127.0.0.1:8090/company",
        }
        first = queue_company_discovery(
            self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
        )
        second = queue_company_discovery(
            self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
        )
        self.assertEqual(first["operation_id"], second["operation_id"])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == first["operation_id"]
            )).mappings().one()
        self.assertEqual(row["kind"], "COMPANY_DISCOVER")
        self.assertEqual(row["params"]["trigger_reason"], "PREVIOUSLY_UNSEEN_COMPANY")
        self.assertEqual(
            row["params"]["candidate"]["trigger_evidence"],
            ["PREVIOUSLY_UNSEEN_COMPANY"],
        )

    def test_durable_company_operation_enrolls_source_and_immediately_imports_employer_only_job(self):
        with fixture_server() as origin:
            candidate = {
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
                "application_url": origin + "/board/apply/A",
            }
            queued = queue_company_discovery(
                self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
            )
            environment = {
                "MJOBS_DATABASE_URL": self.url,
                "MJOBS_MOCK_ORIGIN": origin,
                "MJOBS_OUTPUT_DIR": str(self.root),
                "MJOBS_OPERATION_MODE": "FIXTURE",
            }
            with patch.dict(os.environ, environment, clear=False):
                result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED")
        jobs = self.store.jobs("fixture")
        self.assertEqual({row["requisition"] for row in jobs}, {"A", "B"})
        self.assertEqual(self.store.count("company_job_sources"), 1)
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.company_sources)).mappings().one())
        self.assertTrue(source["discovered_automatically"])
        self.assertEqual(next(row for row in jobs if row["requisition"] == "B")["match_score"], 100)

    def test_career_discovery_finds_navigation_link_without_rel_careers_marker(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/": SimpleNamespace(
                url=origin + "/",
                text='<header><nav><a href="/careers">Jobs and Careers</a></nav></header>',
            ),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<main><a data-provider="mock-ats" data-tenant="employer" href="/ats/jobs">Open roles</a></main>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                self.urls.append(url)
                if url == origin + "/ats/jobs":
                    return SimpleNamespace(url=url, json=lambda: {"jobs": []})
                return pages[url]

        client = FixtureTransport()
        try:
            result = discover_company_source(self.store, {
                "company_name": "Employer Example",
                "company_website": origin + "/",
            }, client)
        except ValueError as failure:
            self.fail(f"career navigation link should be discovered: {failure}")
        self.assertEqual(result["careers_url"], origin + "/careers")
        self.assertEqual(result["provider"], "mock-ats")
        self.assertIn(origin + "/careers", client.urls)

    def test_optional_search_provider_supplies_only_a_bounded_site_candidate(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/directory": SimpleNamespace(
                url=origin + "/directory", text="<main>Employer Example open job listings</main>"
            ),
            origin + "/": SimpleNamespace(
                url=origin + "/",
                text='<h1>Employer Example</h1><header><a href="/careers">Careers</a></header>',
            ),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<a data-provider="mock-ats" data-tenant="employer" href="/ats/jobs">Open roles</a>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                if url == origin + "/ats/jobs":
                    return SimpleNamespace(url=url, json=lambda: {"jobs": []})
                return pages[url]

        class SearchProvider:
            provider_id = "fixture-search"

            def search(self, query, *, limit):
                return [
                    {"url": origin + "/directory", "title": "Employer careers listing", "snippet": "Open roles"},
                    {"url": origin + "/", "title": "Employer Careers", "snippet": "Open roles"},
                ]

        result = discover_company_source(self.store, {"company_name": "Employer Example"},
                                         FixtureTransport(), search_provider=SearchProvider())
        self.assertEqual(result["provider"], "mock-ats")
        self.assertEqual(result["jobs_enumerated"], 0)

    def test_custom_domain_ats_detection_is_audited_without_claiming_application_support(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/": SimpleNamespace(
                url=origin + "/",
                text='<h1>Employer Example</h1><header><a href="/careers">Careers</a></header>',
            ),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<script src="https://boards.greenhouse.io/embed/job_board/js?for=employer"></script>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                return pages[url]

        class SearchProvider:
            provider_id = "fixture-search"

            def search(self, query, *, limit):
                return [{"url": origin + "/", "title": "Employer Careers", "snippet": "Open roles"}]

        with self.assertRaisesRegex(ValueError, "COMPANY_ATS_ENUMERATION_UNSUPPORTED"):
            discover_company_source(self.store, {"company_name": "Employer Example"},
                                    FixtureTransport(), search_provider=SearchProvider())
        with self.store.engine.connect() as connection:
            events = [dict(row) for row in connection.execute(select(m.audit)).mappings().all()]
        event = next(row for row in events if row["action"] == "COMPANY_ATS_DETECTION_REVIEW")
        self.assertEqual(event["payload"]["provider"], "greenhouse")
        self.assertEqual(event["payload"]["visible_careers_url"], origin + "/careers")
        self.assertFalse(event["payload"]["application_supported"])
        self.assertEqual(event["payload"]["discovery_method"], "WEB_SEARCH")
        self.assertEqual(event["payload"]["search_provider"], "fixture-search")
        self.assertEqual(event["payload"]["search_query"], "Employer Example careers jobs")

    def test_custom_domain_ats_signature_corroborates_a_guessed_career_route(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/": SimpleNamespace(url=origin + "/", text="<h1>Employer</h1>"),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<script src="https://boards.greenhouse.io/embed/job_board/js?for=employer"></script>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                if url not in pages:
                    from types import SimpleNamespace
                    error = httpx.HTTPStatusError(
                        "not found", request=httpx.Request("GET", url),
                        response=httpx.Response(404, request=httpx.Request("GET", url)),
                    )
                    raise error
                return pages[url]

        with self.assertRaisesRegex(ValueError, "COMPANY_ATS_ENUMERATION_UNSUPPORTED"):
            discover_company_source(self.store, {
                "company_name": "Employer Example", "company_website": origin + "/",
            }, FixtureTransport())
        with self.store.engine.connect() as connection:
            events = [dict(row) for row in connection.execute(select(m.audit)).mappings().all()]
        event = next(row for row in events if row["action"] == "COMPANY_ATS_DETECTION_REVIEW")
        self.assertEqual(event["payload"]["provider"], "greenhouse")
        self.assertEqual(event["payload"]["visible_careers_url"], origin + "/careers")

    def test_company_discovery_blocks_private_or_off_origin_fetch_targets(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/": SimpleNamespace(url=origin + "/", text='<nav><a href="/careers">Careers</a></nav>'),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<main><a data-provider="mock-ats" data-tenant="employer" href="http://169.254.169.254/latest/meta-data">Open roles</a></main>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                from urllib.parse import urlsplit
                parts = urlsplit(url)
                return (parts.scheme, parts.netloc) == ("https", "employer.example")

            def get(self, url):
                self.urls.append(url)
                return pages[url]

        client = FixtureTransport()
        with self.assertRaisesRegex(ValueError, "COMPANY_DISCOVERY_URL_BLOCKED"):
            discover_company_source(self.store, {
                "company_name": "Employer Example", "company_website": origin + "/",
            }, client)
        self.assertNotIn("http://169.254.169.254/latest/meta-data", client.urls)

    def test_company_discovery_rejects_cross_origin_redirect_result(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        class RedirectingTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                self.urls.append(url)
                return SimpleNamespace(url="http://169.254.169.254/metadata", text="")

        client = RedirectingTransport()
        with self.assertRaisesRegex(ValueError, "COMPANY_DISCOVERY_REDIRECT_BLOCKED"):
            discover_company_source(self.store, {
                "company_name": "Employer Example", "company_website": "https://employer.example/",
            }, client)
        self.assertEqual(client.urls, ["https://employer.example/"])

    def test_career_discovery_accepts_common_route_only_with_ats_content(self):
        from types import SimpleNamespace
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        pages = {
            origin + "/": SimpleNamespace(url=origin + "/", text="<main>Welcome to Employer</main>"),
            origin + "/careers": SimpleNamespace(
                url=origin + "/careers",
                text='<main><a data-provider="mock-ats" data-tenant="employer" href="/ats/jobs">Open roles</a></main>',
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                from urllib.parse import urlsplit
                return (urlsplit(url).scheme, urlsplit(url).netloc) == ("https", "employer.example")

            def get(self, url):
                self.urls.append(url)
                if url == origin + "/ats/jobs":
                    return SimpleNamespace(url=url, json=lambda: {"jobs": []})
                if url not in pages:
                    import httpx
                    request = httpx.Request("GET", url)
                    response = httpx.Response(404, request=request)
                    raise httpx.HTTPStatusError("not found", request=request, response=response)
                return pages[url]

        client = FixtureTransport()
        result = discover_company_source(self.store, {
            "company_name": "Employer Example", "company_website": origin + "/",
        }, client)
        self.assertEqual(result["careers_url"], origin + "/careers")
        self.assertIn(origin + "/careers", client.urls)
    def test_api_can_queue_user_added_company_without_performing_network_io(self):
        with fixture_server() as origin, TestClient(create_app(
            self.store, origin, self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        )) as client:
            response = client.post("/companies/discovery", json={
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
                "reason": "USER_ADDED_COMPANY",
                "profile_id": "fixture",
            })
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "QUEUED")
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == body["operation_id"]
            )).mappings().one()
        self.assertEqual(operation["kind"], "COMPANY_DISCOVER")

    def test_already_monitored_company_is_not_requeued_for_ordinary_trigger(self):
        with fixture_server() as origin:
            candidate = {
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
            }
            company_id = identity(candidate["company_website"])
            with self.store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": company_id, "name": candidate["company_name"],
                    "domain": candidate["company_website"],
                })
                upsert(connection, m.company_sources, {
                    "id": "source", "company_id": company_id,
                    "provider": "mock-ats", "tenant": "example",
                    "endpoint": origin + "/ats/example/jobs",
                    "discovered_automatically": True,
                })
            result = queue_company_discovery(
                self.store, candidate, "HIGH_MATCH_JOB", "fixture"
            )
        self.assertEqual(result["status"], "ALREADY_MONITORED")
        self.assertIsNone(result["operation_id"])


if __name__ == "__main__":
    unittest.main()
