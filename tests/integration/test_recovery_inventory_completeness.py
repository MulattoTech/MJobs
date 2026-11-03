"""Recovery evidence must not accept a truncated public job listing."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from apps.api.main import create_app
from cli import compose_recovery
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_compose_recovery_target import BASELINE, PORT, PROJECT
from tests.integration.test_spine import PROFILE


BUDGET_ERROR = "Public job inventory exceeds the 200-job verification budget"


class PublicJobInventoryCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-recovery-inventory-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'fixture.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.app = create_app(self.store, "http://127.0.0.1:1", self.root,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def seed_jobs(self, count):
        ids = [f"inventory-job-{index:03d}" for index in range(count)]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="inventory-company", name="Synthetic Inventory Employer",
                domain="inventory.example.test"))
            connection.execute(m.jobs.insert(), [{
                "id": job_id, "company_id": "inventory-company", "requisition": str(index),
                "title": "Synthetic Developer", "description": "Controlled inventory fixture",
                "skills": ["Python"], "location": "US", "remote_type": "REMOTE", "attributes": {},
            } for index, job_id in enumerate(ids)])
            connection.execute(m.routes.insert(), [{
                "id": f"inventory-route-{index:03d}", "job_id": job_id,
                "company_id": "inventory-company", "provider": "mock-ats",
                "tenant": "inventory", "requisition": str(index),
                "url": f"http://127.0.0.1:1/mock/ats/apply/{index}",
            } for index, job_id in enumerate(ids)])
            connection.execute(m.postings.insert(), [{
                "id": f"inventory-posting-{index:03d}", "job_id": job_id,
                "source": "synthetic-inventory", "source_job_id": str(index),
                "source_url": f"http://127.0.0.1:1/mock/jobs/{index}",
                "application_url": f"http://127.0.0.1:1/mock/ats/apply/{index}",
                "raw": {"description": "Original controlled source copy"}, "fetched_at": now(),
            } for index, job_id in enumerate(ids)])
        return ids

    def read_json(self, client, calls):
        def get_json(path):
            calls.append(path)
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        return get_json

    def test_api_inventory_refuses_truncated_catalog_before_detail_reads(self):
        ids = self.seed_jobs(201)
        with TestClient(self.app) as client:
            ordinary_page = client.get("/jobs?profile_id=" + PROFILE["id"]).json()["jobs"]
            self.assertEqual(len(ordinary_page), 200)
            self.assertEqual(len(set(ids) - {row["id"] for row in ordinary_page}), 1)
            sentinel_response = client.get("/jobs?profile_id=" + PROFILE["id"] + "&limit=201")
            self.assertEqual(sentinel_response.status_code, 200, sentinel_response.text)
            self.assertEqual({row["id"] for row in sentinel_response.json()["jobs"]}, set(ids))
            calls = []
            with self.assertRaisesRegex(ValueError, "^" + BUDGET_ERROR + "$"):
                compose_recovery.collect_public_job_inventory(
                    self.read_json(client, calls), PROFILE["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(parse_qs(urlsplit(calls[0]).query)["limit"], ["201"])

    def test_api_exact_budget_covers_every_job_and_rejects_changed_source_copy(self):
        ids = self.seed_jobs(200)
        with TestClient(self.app) as client:
            calls = []
            before = compose_recovery.collect_public_job_inventory(
                self.read_json(client, calls), PROFILE["id"])
            self.assertEqual({row["job_id"] for row in before["jobs"]}, set(ids))
            self.assertEqual(before["job_count"], 200)
            self.assertEqual(len(calls), 201)
            self.assertEqual(parse_qs(urlsplit(calls[0]).query)["limit"], ["201"])
            self.assertEqual(compose_recovery.compare_public_job_inventory(
                {"job_inventory": before}, before, before)["result"], "PASS")
            with self.store.engine.begin() as connection:
                connection.execute(m.postings.update().where(
                    m.postings.c.job_id == ids[-1]).values(
                        raw={"description": "Changed controlled source copy"}))
            after = compose_recovery.collect_public_job_inventory(
                self.read_json(client, []), PROFILE["id"])
            with self.assertRaisesRegex(ValueError, "Public job/source inventory changed"):
                compose_recovery.compare_public_job_inventory(
                    {"job_inventory": before}, before, after)

    def test_oversized_response_is_refused_before_detail_reads(self):
        for count in (201, 500):
            with self.subTest(count=count):
                calls = []
                def get_json(path):
                    calls.append(path)
                    if urlsplit(path).path != "/jobs":
                        self.fail("Incomplete inventory must not request job details")
                    return {"jobs": [{"id": f"synthetic-{index}"} for index in range(count)]}
                with self.assertRaisesRegex(ValueError, "^" + BUDGET_ERROR + "$"):
                    compose_recovery.collect_public_job_inventory(get_json, "profile /?&")
                self.assertEqual(len(calls), 1)
                self.assertEqual(parse_qs(urlsplit(calls[0]).query),
                                 {"profile_id": ["profile /?&"], "limit": ["201"]})

    def test_incomplete_inventory_aborts_recovery_before_lifecycle_or_writes(self):
        self.seed_jobs(1)
        with TestClient(self.app) as client:
            inventory = compose_recovery.collect_public_job_inventory(
                self.read_json(client, []), PROFILE["id"])
        # The supplied baseline is internally valid; the current page alone exceeds budget.
        baseline = {**BASELINE, "profile_id": PROFILE["id"], "job_inventory": inventory}
        path = self.root / "baseline.json"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        evidence = self.root / "evidence.json"
        with patch.object(compose_recovery, "preflight_recovery_targets", return_value={}), \
                patch.object(compose_recovery, "resolve_compose_files", return_value=("compose.yml",)), \
                patch.object(compose_recovery, "verify_recovered_application",
                             return_value="/profiles/fixture/applications/synthetic"), \
                patch.object(compose_recovery.httpx, "Client") as transport, \
                patch.object(compose_recovery, "compose") as lifecycle:
            request = transport.return_value.__enter__.return_value.request
            request.return_value.json.return_value = {
                "jobs": [{"id": f"synthetic-{index}"} for index in range(201)]}
            with self.assertRaisesRegex(ValueError, "^" + BUDGET_ERROR + "$"):
                compose_recovery.run(path, evidence, PROJECT, PORT)
            lifecycle.assert_not_called()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "GET")
        self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
