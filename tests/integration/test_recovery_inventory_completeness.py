"""Recovery evidence must not accept a truncated public job listing."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from fastapi.testclient import TestClient

from apps.api.main import create_app
from cli import compose_recovery
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_compose_recovery_target import BASELINE, PORT, PROJECT
from tests.integration.test_spine import PROFILE


BUDGET_ERROR = "Public job inventory exceeds the 200-job verification budget"
LEGACY_ERROR = "Legacy public job inventory requires a fresh complete baseline"


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

    def collect_legacy_version2_inventory(self, client):
        """Freeze the old default-page request and v2 payload, without upgrading it."""
        calls = []
        get_json = self.read_json(client, calls)
        profile = quote(PROFILE["id"], safe="")
        listed = get_json("/jobs?profile_id=" + profile)["jobs"]
        rows = []
        for job_id in sorted(row["id"] for row in listed):
            detail = get_json("/jobs/" + quote(job_id, safe="") + "?profile_id=" + profile)
            canonical = {
                "job_id": job_id,
                "fields": compose_recovery._stable_job_projection({
                    field: detail[field] for field in compose_recovery.CANONICAL_JOB_FIELDS}),
                "routes": sorted(({
                    field: route.get(field) for field in compose_recovery.JOB_ROUTE_FIELDS
                } for route in detail["routes"]), key=lambda route: (
                    str(route["provider"]), str(route["tenant"]),
                    str(route["requisition"]), str(route["id"]))),
            }
            copies = [{
                **{field: posting[field] for field in compose_recovery.SOURCE_COPY_FIELDS},
                "raw_sha256": compose_recovery._inventory_digest(
                    compose_recovery._stable_job_projection(posting.get("raw"))),
            } for posting in detail["postings"]]
            copies.sort(key=lambda row: (
                str(row["source"]), str(row["source_job_id"]), str(row["source_url"])))
            rows.append({
                "job_id": job_id,
                "canonical_sha256": compose_recovery._inventory_digest(canonical),
                "source_copy_count": len(copies),
                "source_copies_sha256": compose_recovery._inventory_digest(copies),
            })
        return {
            "schema_version": 2, "profile_id": PROFILE["id"], "job_count": len(rows),
            "source_copy_count": sum(row["source_copy_count"] for row in rows),
            "jobs": rows, "sha256": compose_recovery._inventory_digest(rows),
        }, calls

    def legacy_baseline_after_omitted_job_loss(self, client):
        ids = self.seed_jobs(201)
        legacy, calls = self.collect_legacy_version2_inventory(client)
        self.assertEqual(legacy["schema_version"], 2)
        self.assertEqual(legacy["job_count"], 200)
        self.assertEqual(len(calls), 201)
        self.assertEqual(parse_qs(urlsplit(calls[0]).query), {"profile_id": [PROFILE["id"]]})
        captured_ids = {row["job_id"] for row in legacy["jobs"]}
        omitted = set(ids) - captured_ids
        self.assertEqual(len(omitted), 1)
        omitted_id = omitted.pop()
        with self.store.engine.begin() as connection:
            connection.execute(m.postings.delete().where(m.postings.c.job_id == omitted_id))
            connection.execute(m.routes.delete().where(m.routes.c.job_id == omitted_id))
            connection.execute(m.jobs.delete().where(m.jobs.c.id == omitted_id))
        calls = []
        complete = compose_recovery.collect_public_job_inventory(
            self.read_json(client, calls), PROFILE["id"])
        self.assertEqual(complete["schema_version"], 3)
        self.assertEqual(complete["job_count"], 200)
        self.assertEqual({row["job_id"] for row in complete["jobs"]}, set(ids) - {omitted_id})
        self.assertEqual(parse_qs(urlsplit(calls[0]).query)["limit"], ["201"])
        self.assertEqual(complete["jobs"], legacy["jobs"])
        # Equal row hashes cannot establish that the historical page was complete.
        self.assertEqual(complete["sha256"], legacy["sha256"])
        return legacy, complete

    def test_legacy_default_page_cannot_certify_recovery_after_omitted_job_loss(self):
        with TestClient(self.app) as client:
            legacy, complete = self.legacy_baseline_after_omitted_job_loss(client)
        self.assertEqual(compose_recovery.compare_public_job_inventory(
            {"job_inventory": complete}, complete, complete)["result"], "PASS")
        with self.assertRaisesRegex(ValueError, "^" + LEGACY_ERROR + "$"):
            compose_recovery.compare_public_job_inventory(
                {"job_inventory": legacy}, complete, complete)

    def test_legacy_default_page_aborts_recovery_before_target_or_http_access(self):
        with TestClient(self.app) as client:
            legacy, _ = self.legacy_baseline_after_omitted_job_loss(client)
        baseline = {**BASELINE, "profile_id": PROFILE["id"], "job_inventory": legacy}
        path = self.root / "legacy-baseline.json"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        evidence = self.root / "legacy-recovery-evidence.json"
        with patch.object(compose_recovery, "resolve_compose_files",
                          side_effect=AssertionError("Legacy baseline reached target resolution")) as resolver, \
                patch.object(compose_recovery, "preflight_recovery_targets",
                             side_effect=AssertionError("Legacy baseline reached target preflight")) as preflight, \
                patch.object(compose_recovery.httpx, "Client",
                             side_effect=AssertionError("Legacy baseline reached HTTP access")) as transport, \
                patch.object(compose_recovery, "compose",
                             side_effect=AssertionError("Legacy baseline reached service mutation")) as lifecycle:
            with self.assertRaisesRegex(ValueError, "^" + LEGACY_ERROR + "$"):
                compose_recovery.run(path, evidence, PROJECT, PORT)
            resolver.assert_not_called()
            preflight.assert_not_called()
            transport.assert_not_called()
            lifecycle.assert_not_called()
        self.assertFalse(evidence.exists())

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
            self.assertEqual(before["schema_version"], 3)
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
