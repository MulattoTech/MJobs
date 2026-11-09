"""Both operation views preserve exact profile ownership before pagination."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.monitoring import configure_monitoring, queue_refresh, source_version
from services.company_discovery.sources import upsert_company_job_source
from workers.operations import claim, recover_expired


VIEWS = ("/operations", "/operations/dead-letter-queue")
AT = "2026-11-09T00:00:00+00:00"


class OperationProfileScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-operation-scope-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database_url = self._database_url()
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self._initialize_store()
        self.store.bind_workspace_mode("PERSONAL")
        for profile_id in ("fixture", "other", "fixture-other", "Fixture", "123"):
            self.store.save_profile({
                "id": profile_id, "name": "Synthetic User", "email": "user@example.test",
                "answers": [], "facts": [],
            })
        self.client = self._client()

    def _database_url(self):
        return f"sqlite:///{(self.root / 'scope.db').as_posix()}"

    def _initialize_store(self):
        self.store.migrate()

    def _client(self):
        client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        self.addCleanup(client.close)
        return client

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _get(self, endpoint, profile_id=None):
        before = self._snapshot()
        params = {} if profile_id is None else {"profile_id": profile_id}
        response = self.client.get(endpoint, params=params)
        self.assertEqual(self._snapshot(), before, "Operation GET changed durable state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _assert_views(self, profile_id, expected):
        for endpoint in VIEWS:
            with self.subTest(endpoint=endpoint, profile_id=profile_id):
                body = self._get(endpoint, profile_id)
                self.assertEqual({row["id"] for row in body["operations"]}, set(expected))
                if endpoint.endswith("dead-letter-queue"):
                    self.assertEqual(body["count"], len(expected))

    def _operation(self, label, params, *, kind="COMPANY_SOURCE_REFRESH", entity_id=None,
                   state="DEAD_LETTERED", updated_at=AT, error="RETRY_LIMIT"):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=label, kind=kind, entity_id=entity_id or "source-" + label,
                params=params, state=state, error=error, updated_at=updated_at,
                attempt_count=3, last_attempt_at=updated_at,
                dead_lettered_at=updated_at if state == "DEAD_LETTERED" else None))
        return label

    def _related_records(self):
        """Minimal persisted relationships; no collector, renderer or submission."""
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Synthetic Employer", domain="employer.example"))
            connection.execute(m.jobs.insert().values(
                id="job", company_id="company", requisition="job", title="Synthetic role",
                description="Fixture only", skills=[], location="Remote", remote_type="REMOTE_US"))
            connection.execute(m.routes.insert().values(
                id="route", job_id="job", company_id="company", provider="mock",
                tenant="synthetic", requisition="job", url="https://employer.example/jobs/1"))
            for profile_id in ("fixture", "other"):
                version = connection.execute(select(m.profiles.c.version).where(
                    m.profiles.c.id == profile_id)).scalar_one()
                connection.execute(m.applications.insert().values(
                    id="app-" + profile_id, job_id="job", route_id="route",
                    profile_id=profile_id, profile_version=version,
                    state="SUBMISSION_UNCONFIRMED", target="MOCK", fact_ids=[], trace={},
                    created_at=AT))
                connection.execute(m.uploaded_resumes.insert().values(
                    id="upload-" + profile_id, profile_id=profile_id,
                    filename="synthetic.txt", format="txt", media_type="text/plain",
                    size_bytes=1, sha256="a" * 64, status="UPLOADED", created_at=AT))

    def test_real_refresh_exhaustion_is_visible_to_owner_after_restart(self):
        company_id = identity("public-company-origin", "https://employer.example")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Synthetic Employer", "domain": "",
            })
        source = upsert_company_job_source(
            self.store, company_id=company_id, provider="json_feed",
            tenant_identifier=company_id, career_url="https://employer.example/careers",
            public_api_url="https://employer.example/jobs.json", collection_strategy="PUBLIC_JSON",
            enabled=True, discovered_automatically=True, confidence=0.45)
        configure_monitoring(self.store, source["id"], source_version(source), 0, 3600)
        identifier = queue_refresh(self.store, source["id"], "fixture")["operation_id"]
        for attempt in range(1, 4):
            self.assertIsNotNone(claim(self.store, identifier))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                    lease_expires_at="2000-01-01T00:00:00+00:00"))
            recover_expired(self.store)
            with self.store.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.id == identifier)).mappings().one()
            self.assertEqual(row["state"], "QUEUED" if attempt < 3 else "DEAD_LETTERED")
            self.assertEqual(row["attempt_count"], attempt)
        self.assertEqual(row["entity_id"], source["id"])
        self.assertEqual(row["params"]["profile_ids"], ["fixture"])
        self.assertEqual(row["error"], "RETRY_LIMIT")
        self.assertTrue(row["last_attempt_at"] and row["dead_lettered_at"])
        self._assert_views(None, {identifier})
        self._assert_views("fixture", {identifier})
        self._assert_views("other", set())
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.client = self._client()
        self._assert_views("fixture", {identifier})
        rows = self._get(VIEWS[1], "fixture")["operations"]
        self.assertEqual(len(rows), 1)
        item = rows[0]
        self.assertTrue(item["retryable"])
        self.assertEqual(item["error"], "RETRY_LIMIT")

    def test_shared_refresh_uses_exact_string_membership(self):
        shared = self._operation("shared", {"profile_ids": ["fixture", "other", "fixture"]})
        longer = self._operation("longer", {"profile_ids": ["fixture-other"]})
        capital = self._operation("capital", {"profile_ids": ["Fixture"]})
        numeric_string = self._operation("numeric-string", {"profile_ids": ["123"]})
        self._operation("numeric-value", {"profile_ids": [123]})
        for profile_id, expected in (
            ("fixture", {shared}), ("other", {shared}), ("fixture-other", {longer}),
            ("Fixture", {capital}), ("123", {numeric_string}),
        ):
            self._assert_views(profile_id, expected)

    def test_malformed_refresh_ownership_and_entity_collisions_fail_closed(self):
        self._related_records()
        malformed = [
            {}, None, [], "fixture", {"profile_ids": None}, {"profile_ids": "fixture"},
            {"profile_ids": {"fixture": True}}, {"profile_ids": 123},
            {"profile_ids": True}, {"profile_ids": []}, {"profile_ids": ["fixture", 7]},
            {"profile_ids": ["fixture", None]}, {"profile_ids": ["fixture", True]},
            {"profile_ids": ["fixture", {"id": "other"}]},
            {"profile_ids": ["fixture", ["other"]]},
        ]
        for index, params in enumerate(malformed):
            self._operation(f"malformed-{index}", params, entity_id="fixture")
        for index, entity_id in enumerate(("fixture", "app-fixture", "upload-fixture")):
            self._operation(f"foreign-collision-{index}", {"profile_ids": ["other"]},
                            entity_id=entity_id)
            self._operation(f"missing-collision-{index}", {}, entity_id=entity_id)
        self._assert_views("fixture", set())
        self._assert_views("other", {f"foreign-collision-{index}" for index in range(3)})

    def test_profile_array_does_not_grant_ownership_to_other_operation_kinds(self):
        for kind in ("DISCOVER", "PREPARE", "RESUME_EXTRACT"):
            self._operation(kind, {"profile_ids": ["fixture"]}, kind=kind)
        other_discovery = self._operation("company-discovery", {
            "profile_ids": ["fixture"], "profile_id": "other",
        }, kind="COMPANY_DISCOVER")
        self._assert_views("fixture", set())
        self._assert_views("other", {other_discovery})

    def test_profile_filter_precedes_recent_limit_and_dead_letter_count(self):
        own, foreign = set(), set()
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for index in range(105):
            own.add(self._operation(f"own-{index}", {"profile_ids": ["fixture"]},
                updated_at=(start + timedelta(seconds=index)).isoformat()))
            foreign.add(self._operation(f"foreign-{index}", {"profile_ids": ["other"]},
                updated_at=(start + timedelta(days=1, seconds=index)).isoformat()))
        for profile_id, identifiers in (("fixture", own), ("other", foreign)):
            for endpoint in VIEWS:
                with self.subTest(profile_id=profile_id, endpoint=endpoint):
                    body = self._get(endpoint, profile_id)
                    self.assertEqual(len(body["operations"]), 100)
                    self.assertTrue({row["id"] for row in body["operations"]} <= identifiers)
                    if endpoint == VIEWS[1]:
                        self.assertEqual(body["count"], 105)
        unfiltered = self._get(VIEWS[1])
        self.assertEqual(unfiltered["count"], 210)
        self.assertEqual(len(unfiltered["operations"]), 100)

    def test_legacy_relationships_and_ambiguous_submission_guard_are_preserved(self):
        self._related_records()
        for profile_id in ("fixture", "other"):
            expected = {
                self._operation("direct-" + profile_id, {}, kind="DISCOVER", entity_id=profile_id),
                self._operation("application-" + profile_id, {"submit": True},
                    kind="APPLY_MOCK", entity_id="app-" + profile_id),
                self._operation("resume-" + profile_id, {}, kind="RESUME_EXTRACT",
                    entity_id="upload-" + profile_id),
                self._operation("discovery-" + profile_id, {"profile_id": profile_id},
                    kind="COMPANY_DISCOVER", entity_id="company"),
            }
            self._assert_views(profile_id, expected)
            item = next(row for row in self._get(VIEWS[1], profile_id)["operations"]
                        if row["kind"] == "APPLY_MOCK")
            self.assertFalse(item["retryable"])
            self.assertEqual(item["retry_blocked_reason"], "RECONCILIATION_REQUIRED")
        # Check the first profile again after the second profile has durable rows.
        self._assert_views("fixture", {
            "direct-fixture", "application-fixture", "resume-fixture", "discovery-fixture",
        })

    def test_held_manual_auth_remains_reachable_beyond_recent_history(self):
        for index in range(101):
            self._operation(f"history-{index}", {}, kind="DISCOVER", entity_id="fixture",
                            state="COMPLETED", error=None)
        held = {
            self._operation("held-running", {}, kind="MANUAL_AUTH", entity_id="fixture",
                            state="RUNNING", updated_at="2000-01-01", error=None),
            self._operation("held-blocked", {}, kind="MANUAL_AUTH", entity_id="fixture",
                            state="BLOCKED", updated_at="2000-01-01",
                            error="BROWSER_RELEASE_UNCONFIRMED"),
        }
        self._operation("foreign-held", {}, kind="MANUAL_AUTH", entity_id="other",
                        state="RUNNING", updated_at="2000-01-01", error=None)
        self._operation("ordinary-blocked", {}, kind="MANUAL_AUTH", entity_id="fixture",
                        state="BLOCKED", updated_at="2000-01-01", error="OTHER_REASON")
        body = self._get(VIEWS[0], "fixture")
        identifiers = [row["id"] for row in body["operations"]]
        self.assertEqual(len(identifiers), 102)
        self.assertEqual(len(set(identifiers)), 102)
        self.assertTrue(held <= set(identifiers))
        self.assertNotIn("foreign-held", identifiers)
        self.assertNotIn("ordinary-blocked", identifiers)
        self.assertEqual(self._get(VIEWS[1], "fixture"), {"count": 0, "operations": []})


if __name__ == "__main__":
    unittest.main()
