"""Public source enrollment stays closed, canonical, and distinct from ATS identity."""
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.dedupe.routes import qualify_route_authority
from services.discovery.sources.collect import collect_source
from services.discovery.sources.remotive import Remotive


class SourceEnrollmentApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-source-enrollment-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_api_enrolls_only_registered_sources_with_canonical_global_tenant(self):
        for provider in ("remotive", "remoteok", "wwr"):
            response = self.client.put("/sources", json={
                "provider": provider, "tenant": "default",
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"id": identity(provider, "default")})

        rows = self.client.get("/sources").json()["sources"]
        configured = {row["provider"]: row for row in rows}
        self.assertEqual(set(configured), {"remotive", "remoteok", "wwr"})
        self.assertEqual(configured["remotive"]["strategy"], "PUBLIC_JSON")
        self.assertEqual(configured["remoteok"]["strategy"], "PUBLIC_JSON")
        self.assertEqual(configured["wwr"]["strategy"], "FEED")
        self.assertTrue(all(row["tenant"] == "default" for row in configured.values()))

        # Legacy clients that omit provider keep creating explicit Greenhouse boards.
        legacy = self.client.put("/sources", json={"tenant": "example-board"})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json(), {"id": identity("greenhouse", "example-board")})

    def test_unknown_provider_and_noncanonical_global_tenant_fail_without_writes(self):
        for payload in (
            {"provider": "unknown-board", "tenant": "default"},
            {"provider": "remotive", "tenant": "custom"},
            {"provider": "remoteok", "tenant": "https://attacker.example/feed"},
            {"provider": "wwr", "tenant": "default", "rate_limit": 0},
            {"provider": "remotive", "tenant": "default", "request_timeout": 1},
            {"provider": "remoteok", "tenant": "default", "poll_interval": 299},
            {"provider": "wwr", "tenant": "default", "concurrency": 9},
            {"provider": "remotive", "tenant": "default", "enabled": "false"},
        ):
            response = self.client.put("/sources", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.count("job_sources"), 0)

    def test_internal_remoteok_alias_enrolls_the_canonical_source_once(self):
        from services.discovery.sources.collect import configure_source

        canonical = configure_source(self.store, "default", provider="remoteok")
        alias = configure_source(self.store, "default", provider="remote_ok")
        self.assertEqual(alias, canonical)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.job_sources)).mappings().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "remoteok")

    def test_global_collection_keeps_aggregator_provenance_without_claiming_ats_or_employer_domain(self):
        from tests.integration.test_spine import PROFILE

        self.store.save_profile(dict(PROFILE))
        source_id = self.client.put("/sources", json={
            "provider": "remotive", "tenant": "default",
        }).json()["id"]
        payload = {"jobs": [{
            "id": 701,
            "url": "https://remotive.com/remote-jobs/software-dev/backend-engineer-701",
            "title": "Backend Engineer",
            "company_name": "Example Employer",
            "candidate_required_location": "Worldwide",
            "description": "Build Python services and cloud APIs.",
            "tags": ["python", "api"],
            "job_type": "full_time",
        }, {
            "id": 702,
            "url": "https://remotive.com/remote-jobs/software-dev/sre-702",
            "title": "Site Reliability Engineer",
            "company_name": "New Employer",
            "candidate_required_location": "Worldwide",
            "description": "Operate cloud systems and automate reliability.",
            "tags": ["cloud", "automation"],
            "job_type": "full_time",
        }]}

        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": identity("remotive", "example-employer"),
                "name": "Curated Employer Name",
                "domain": "https://employer.example",
            })

        def transport(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, "remotive.com")
            self.assertEqual(request.url.path, "/api/remote-jobs")
            return httpx.Response(200, json=payload)

        collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(transport))

        with self.store.engine.connect() as connection:
            tenants = connection.execute(select(m.company_ats_tenants)).mappings().all()
            company_sources = connection.execute(select(m.company_sources)).mappings().all()
            companies = connection.execute(select(m.companies)).mappings().all()
            routes = [dict(row) for row in connection.execute(select(m.routes)).mappings()]
            authorities = qualify_route_authority(connection, routes)

        self.assertEqual(tenants, [])
        self.assertEqual(len(company_sources), 2)
        self.assertTrue(all(row["ats_tenant_id"] is None for row in company_sources))
        self.assertTrue(all(row["provider"] == "remotive" for row in company_sources))
        self.assertTrue(all(row["tenant"] == "default" for row in company_sources))
        self.assertTrue(all(row["collector_source_id"] == source_id for row in company_sources))
        self.assertEqual({row["id"]: row["domain"] for row in companies}, {
            identity("remotive", "example-employer"): "https://employer.example",
            identity("remotive", "new-employer"): "",
        })
        self.assertEqual({row["id"]: row["name"] for row in companies}, {
            identity("remotive", "example-employer"): "Curated Employer Name",
            identity("remotive", "new-employer"): "New Employer",
        })
        self.assertEqual(len(authorities), 2)
        self.assertTrue(all(row["authority"] == "TRUSTED_INTEGRATED_BOARD" for row in authorities))
        self.assertTrue(all(row["authority"] not in {"VERIFIED_ATS", "VERIFIED_EMPLOYER"}
                            for row in authorities))


if __name__ == "__main__":
    unittest.main()
