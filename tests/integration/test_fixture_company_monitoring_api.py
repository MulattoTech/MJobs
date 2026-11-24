"""Fixture monitoring routes are registered and read back through the app."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import source_version
from services.discovery.pipeline import discover
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE


class FixtureCompanyMonitoringApiTests(unittest.TestCase):
    def test_registered_fixture_monitor_api_and_source_view_are_scoped_and_truthful(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory(
                prefix="mjobs-fixture-monitoring-api-"))
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            stack.callback(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(PROFILE)
            save_search_profile(store, dict(
                id="reviewed", profile_id="fixture", name="Reviewed",
                enabled=True, source_ids=[],
                scheduler={"enabled": False, "interval_seconds": 300},
            ))
            store.save_profile(dict(PROFILE, id="foreign"))

            origin = stack.enter_context(fixture_server())
            discover(store, origin, "fixture")
            with store.engine.connect() as connection:
                source = dict(connection.execute(select(m.company_sources)).mappings().one())
            source_sha = source_version(source)
            route = f"/profiles/fixture/company-sources/{source['id']}/fixture-monitoring"
            body = {"source_version": source_sha, "revision": 0, "poll_interval": 300}

            with TestClient(create_app(
                    store, origin, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as api:
                listed = api.get("/company-sources/view", params={"profile_id": "fixture"})
                self.assertEqual(listed.status_code, 200, listed.text)
                source_view = next(row for row in listed.json()["company_sources"]
                                   if row["id"] == source["id"])
                self.assertTrue(source_view["monitoring_supported"])
                self.assertIsNone(source_view["monitoring"])
                foreign_listed = api.get(
                    "/company-sources/view", params={"profile_id": "foreign"}
                ).json()["company_sources"]
                foreign_source_view = next(row for row in foreign_listed
                                           if row["id"] == source["id"])
                self.assertFalse(foreign_source_view["monitoring_supported"])

                configured = api.put(route, json=body)
                self.assertEqual(configured.status_code, 200, configured.text)
                configured_value = configured.json()
                self.assertEqual(configured_value["source_id"], source["id"])
                self.assertEqual(configured_value["profile_id"], "fixture")
                self.assertEqual(configured_value["revision"], 1)
                self.assertEqual(configured_value["status"], "IDLE")
                self.assertFalse(configured_value["external_submission_enabled"])

                readback = api.get(route)
                self.assertEqual(readback.status_code, 200, readback.text)
                self.assertEqual(readback.json(), configured_value)
                listed = api.get("/company-sources/view", params={"profile_id": "fixture"})
                source_view = next(row for row in listed.json()["company_sources"]
                                   if row["id"] == source["id"])
                self.assertTrue(source_view["monitoring_supported"])
                self.assertEqual(source_view["monitoring"]["status"], "IDLE")
                self.assertEqual(source_view["monitoring"]["revision"], 1)
                self.assertEqual(source_view["next_check"], configured_value["next_due"])

                stale_revision = api.put(route, json=body)
                self.assertEqual(stale_revision.status_code, 409)
                self.assertEqual(stale_revision.json()["detail"], "FIXTURE_MONITOR_REVISION_CONFLICT")
                stale_source = api.put(route, json={**body, "source_version": "0" * 64,
                                                    "revision": 1})
                self.assertEqual(stale_source.status_code, 409)
                self.assertEqual(stale_source.json()["detail"], "FIXTURE_MONITOR_SOURCE_VERSION_CONFLICT")
                missing_source = api.put(
                    f"/profiles/fixture/company-sources/missing-source/fixture-monitoring", json=body)
                self.assertEqual(missing_source.status_code, 409)
                self.assertEqual(missing_source.json()["detail"], "FIXTURE_MONITOR_RECORD_NOT_FOUND")
                foreign_profile = api.put(
                    f"/profiles/foreign/company-sources/{source['id']}/fixture-monitoring", json=body)
                self.assertEqual(foreign_profile.status_code, 409)
                self.assertEqual(foreign_profile.json()["detail"],
                                 "FIXTURE_MONITOR_PROFILE_SOURCE_MISMATCH")

                store.save_profile(dict(PROFILE, name="Changed after review"))
                stale_readback = api.get(route)
                self.assertEqual(stale_readback.status_code, 409)
                self.assertEqual(stale_readback.json()["detail"], "FIXTURE_MONITOR_PROFILE_CHANGED")
                listed = api.get("/company-sources/view", params={"profile_id": "fixture"})
                source_view = next(row for row in listed.json()["company_sources"]
                                   if row["id"] == source["id"])
                self.assertEqual(source_view["monitoring"]["status"], "BLOCKED")
                self.assertEqual(source_view["monitoring"]["last_error"],
                                 "FIXTURE_MONITOR_PROFILE_CHANGED")

            personal_store = Store(f"sqlite:///{(root / 'personal.db').as_posix()}")
            stack.callback(personal_store.engine.dispose)
            personal_store.migrate()
            personal_store.bind_workspace_mode("PERSONAL")
            personal_store.save_profile(PROFILE)
            discover(personal_store, origin, "fixture")
            with TestClient(create_app(
                    personal_store, origin, root, "redis://127.0.0.1:1/0",
                    operation_mode="PERSONAL")) as personal_api:
                rejected = personal_api.put(route, json=body)
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(rejected.json()["detail"], "FIXTURE_MONITOR_WORKSPACE_REQUIRED")
                personal_listing = personal_api.get(
                    "/company-sources/view", params={"profile_id": "fixture"}
                ).json()["company_sources"]
                personal_source = next(row for row in personal_listing if row["id"] == source["id"])
                self.assertFalse(personal_source["monitoring_supported"])


if __name__ == "__main__":
    unittest.main()
