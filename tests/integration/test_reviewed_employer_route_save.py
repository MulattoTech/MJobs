"""Explicitly saved redirect destinations become durable, selectable routes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class ReviewedEmployerRouteSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-reviewed-route-save-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'routes.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile = copy.deepcopy(PROFILE)
        self.store.save_profile(self.profile)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "route-save-company", "name": "Route Save Employer",
                "domain": "routesave.example",
            })
            upsert(connection, m.jobs, {
                "id": "route-save-job", "company_id": "route-save-company",
                "requisition": "req-7", "title": "Systems Engineer",
                "description": "Support systems.", "skills": [], "location": "Remote",
                "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "route-save-aggregator", "job_id": "route-save-job",
                "company_id": "route-save-company", "provider": "job-board",
                "tenant": "", "requisition": "req-7",
                "url": "https://board.example/jobs/req-7",
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "route-save-tenant", "company_id": "route-save-company",
                "provider": "greenhouse", "tenant_identifier": "routesave",
                "organization_identifier": "", "board_identifier": "",
                "confidence": 0.95, "evidence": ["PUBLIC_API_BOARD"],
                "first_seen_at": "2026-10-09T00:00:00Z",
                "last_seen_at": "2026-10-09T00:00:00Z",
            })
        self.app = create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def client(self):
        return TestClient(self.app)

    def route_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.routes)).scalar_one()

    def verified_ats_result(self):
        return {
            "status": "RESOLVED_ATS_ROUTE",
            "candidate_url": "https://boards.greenhouse.io/routesave/jobs/77",
            "provider": "greenhouse", "tenant": "routesave",
        }

    def test_explicit_save_rechecks_and_persists_a_verified_route_without_selecting_it(self):
        with patch("apps.api.main.resolve_aggregator_redirect",
                   return_value=self.verified_ats_result()) as resolve:
            with self.client() as client:
                response = client.post(
                    "/jobs/route-save-job/routes/route-save-aggregator/"
                    "save-verified-destination",
                )
                self.assertEqual(response.status_code, 201, response.text)
                first = response.json()
                self.assertEqual(first["status"], "RESOLVED_ATS_ROUTE")
                self.assertTrue(first["created"])
                self.assertEqual(first["route"]["provider"], "greenhouse")
                self.assertEqual(first["route"]["url"], self.verified_ats_result()["candidate_url"])
                self.assertEqual(resolve.call_count, 1)

                replay = client.post(
                    "/jobs/route-save-job/routes/route-save-aggregator/"
                    "save-verified-destination",
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertFalse(replay.json()["created"])
                self.assertEqual(replay.json()["route"]["id"], first["route"]["id"])
                self.assertEqual(resolve.call_count, 2)

                detail = client.get(f"/jobs/route-save-job?profile_id={self.profile['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                routes = {row["id"]: row for row in detail.json()["routes"]}
                self.assertEqual(set(routes), {"route-save-aggregator", first["route"]["id"]})
                self.assertEqual(routes[first["route"]["id"]]["authority"], "VERIFIED_ATS")
                self.assertFalse(routes[first["route"]["id"]]["preferred_by_profile"])
                self.assertTrue(routes["route-save-aggregator"]["authority"] == "AGGREGATOR")
                self.assertEqual(detail.json()["attributes"].get("source_resolution"), None)
        self.assertEqual(self.route_count(), 2)
        with self.store.engine.connect() as connection:
            events = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == "route-save-job",
                m.audit.c.action == "APPLICATION_ROUTE_DESTINATION_SAVED",
            )).mappings().all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["source_route_id"], "route-save-aggregator")
        self.assertEqual(events[0]["payload"]["destination_route_id"], first["route"]["id"])

    def test_read_only_route_check_never_persists_a_verified_destination(self):
        with patch("apps.api.main.resolve_aggregator_redirect",
                   return_value=self.verified_ats_result()):
            with self.client() as client:
                response = client.post(
                    "/jobs/route-save-job/routes/route-save-aggregator/resolve",
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "RESOLVED_ATS_ROUTE")
        self.assertEqual(self.route_count(), 1)
        with self.store.engine.connect() as connection:
            events = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.entity_id == "route-save-job",
                m.audit.c.action == "APPLICATION_ROUTE_DESTINATION_SAVED",
            )).scalar_one()
        self.assertEqual(events, 0)

    def test_unresolved_or_unverified_destination_is_not_saved(self):
        cases = [
            {"status": "UNRESOLVED"},
            {"status": "BLOCKED", "reason": "ROBOTS_DENIED"},
            {
                "status": "RESOLVED_ATS_ROUTE",
                "candidate_url": "https://boards.greenhouse.io/other/jobs/77",
                "provider": "greenhouse", "tenant": "other",
            },
            {
                "status": "RESOLVED_ATS_ROUTE",
                "candidate_url": "http://boards.greenhouse.io/routesave/jobs/78",
                "provider": "greenhouse", "tenant": "routesave",
            },
            {
                "status": "RESOLVED_ATS_ROUTE",
                "candidate_url": "https://boards.greenhouse.io/routesave/jobs/78?token=private",
                "provider": "greenhouse", "tenant": "routesave",
            },
            {
                "status": "RESOLVED_ATS_ROUTE",
                "candidate_url": "https://boards.greenhouse.io/routesave/jobs/"
                + ("x" * 2000),
                "provider": "greenhouse", "tenant": "routesave",
            },
        ]
        with patch("apps.api.main.resolve_aggregator_redirect", side_effect=cases):
            with self.client() as client:
                for _ in cases:
                    response = client.post(
                        "/jobs/route-save-job/routes/route-save-aggregator/"
                        "save-verified-destination",
                    )
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["detail"], "ROUTE_DESTINATION_NOT_VERIFIED")
        self.assertEqual(self.route_count(), 1)

    def test_matching_tenant_metadata_cannot_verify_a_different_url_identity(self):
        cases = [
            'https://boards.greenhouse.io/other/jobs/77',
            'https://boards.greenhouse.io/other/jobs/routesave',
            'https://other.example/routesave/jobs/77',
            'https://boards.greenhouse.io/routesave/../other/jobs/77',
            'https://boards.greenhouse.io/routesave/%2e%2e/other/jobs/77',
        ]
        with self.client() as client:
            for url in cases:
                with self.subTest(url=url), patch('apps.api.main.resolve_aggregator_redirect',
                        return_value={**self.verified_ats_result(), 'candidate_url': url}):
                    response = client.post('/jobs/route-save-job/routes/route-save-aggregator/'
                                           'save-verified-destination')
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()['detail'], 'ROUTE_DESTINATION_NOT_VERIFIED')
        self.assertEqual(self.route_count(), 1)

    def test_persisted_wrong_greenhouse_url_is_not_presented_as_verified(self):
        from services.dedupe.routes import load_job_routes
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {'id': 'wrong-employer-route',
                'job_id': 'route-save-job', 'company_id': 'route-save-company',
                'provider': 'greenhouse', 'tenant': 'routesave', 'requisition': 'wrong-77',
                'url': 'https://boards.greenhouse.io/other/jobs/77'})
        route = next(row for row in load_job_routes(self.store, 'route-save-job')
                     if row['id'] == 'wrong-employer-route')
        self.assertEqual(route['authority'], 'UNKNOWN')

    def test_lever_save_and_readback_bind_the_current_regional_site(self):
        from services.dedupe.routes import load_job_routes
        posting = '00000000-0000-0000-0000-000000000001'
        with self.client() as client:
            for region, host, foreign in [('global', 'jobs.lever.co', 'jobs.eu.lever.co'),
                                          ('eu', 'jobs.eu.lever.co', 'jobs.lever.co')]:
                tenant = region + ':routesave'
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.company_ats_tenants).values(
                        provider='lever', tenant_identifier=tenant))
                result = {'status': 'RESOLVED_ATS_ROUTE', 'provider': 'lever', 'tenant': tenant,
                          'candidate_url': f'https://{host}/routesave/{posting}'}
                for url in (f'https://{foreign}/routesave/{posting}',
                            f'https://{host}/other/{posting}',
                            f'https://foreign.example/routesave/{posting}',
                            f'https://{host}:443/routesave/{posting}'):
                    with self.subTest(region=region, url=url), patch(
                            'apps.api.main.resolve_aggregator_redirect',
                            return_value={**result, 'candidate_url': url}):
                        response = client.post('/jobs/route-save-job/routes/route-save-aggregator/'
                                               'save-verified-destination')
                        self.assertEqual(response.status_code, 409, response.text)
                        with self.store.engine.begin() as connection:
                            upsert(connection, m.routes, {'id': 'invalid-stored-lever',
                                'job_id': 'route-save-job', 'company_id': 'route-save-company',
                                'provider': 'lever', 'tenant': tenant, 'requisition': 'invalid-lever',
                                'url': url}, replace=True)
                        self.assertEqual(next(row for row in load_job_routes(self.store, 'route-save-job')
                                              if row['id'] == 'invalid-stored-lever')['authority'], 'UNKNOWN')
                with patch('apps.api.main.resolve_aggregator_redirect', return_value=result):
                    response = client.post('/jobs/route-save-job/routes/route-save-aggregator/'
                                           'save-verified-destination')
                    self.assertEqual(response.status_code, 201, response.text)
                    saved_id = response.json()['route']['id']
                self.assertEqual(next(row for row in load_job_routes(self.store, 'route-save-job')
                                      if row['id'] == saved_id)['authority'], 'VERIFIED_ATS')

    def test_identity_evidence_revoked_during_recheck_blocks_route_creation(self):
        def revoke_tenant(_url, **_kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(m.company_ats_tenants.delete().where(
                    m.company_ats_tenants.c.id == "route-save-tenant",
                ))
            return self.verified_ats_result()

        with patch("apps.api.main.resolve_aggregator_redirect", side_effect=revoke_tenant):
            with self.client() as client:
                response = client.post(
                    "/jobs/route-save-job/routes/route-save-aggregator/"
                    "save-verified-destination",
                )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "ROUTE_DESTINATION_NOT_VERIFIED")
        self.assertEqual(self.route_count(), 1)

    def test_only_a_route_belonging_to_the_requested_job_can_be_saved(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id": "route-save-known-ats", "job_id": "route-save-job",
                "company_id": "route-save-company", "provider": "greenhouse",
                "tenant": "routesave", "requisition": "req-7",
                "url": "https://boards.greenhouse.io/routesave/jobs/77",
            })
        with self.client() as client:
            missing = client.post(
                "/jobs/route-save-job/routes/foreign-route/save-verified-destination",
            )
            self.assertEqual(missing.status_code, 404)
            non_aggregator = client.post(
                "/jobs/route-save-job/routes/route-save-known-ats/save-verified-destination",
            )
            self.assertEqual(non_aggregator.status_code, 409)
        self.assertEqual(self.route_count(), 2)


if __name__ == "__main__":
    unittest.main()
