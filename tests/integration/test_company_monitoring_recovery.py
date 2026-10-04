"""Recovery, authorization, and receipt boundaries for company-source monitoring."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.monitoring import configure_monitoring, queue_refresh, schedule_company_sources, source_version
from services.company_discovery.public_client import HTTPStatusError, PublicCompanyClient
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.operations import request_cancel
from workers.tasks import execute_operation


class MonitoringFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'monitor.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        save_search_profile(self.store, dict(id="all", profile_id="fixture", name="All sources", source_ids=[]))
        self.company = identity("public-company-origin", "https://employer.example")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": self.company, "name": "Employer", "domain": ""})
        self.source = upsert_company_job_source(
            self.store, company_id=self.company, provider="json_feed", tenant_identifier=self.company,
            career_url="https://employer.example/careers", public_api_url="https://employer.example/jobs.json",
            collection_strategy="PUBLIC_JSON", enabled=True, discovered_automatically=True, confidence=.45)
        self.env = dict(MJOBS_DATABASE_URL=self.store.url, MJOBS_OPERATION_MODE="PERSONAL",
                        MJOBS_REDIS_URL="redis://127.0.0.1:1/0", MJOBS_OUTPUT_DIR=str(self.root / "out"))
        self.pages = {
            "/robots.txt": "User-agent: *\nAllow: /",
            "/": '<a rel="careers" href="/careers">Careers</a>',
            "/careers": '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            "/jobs.json": json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
                {"id": "one", "title": "Python Engineer", "content_text": "Python SQL",
                 "url": "https://employer.example/jobs/one"}]}),
            "/jobs/one": "<html>No structured identity</html>",
        }

    def close(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def fetch(self, url, deadline):
        request = httpx.Request("GET", url)
        return httpx.Response(200, text=self.pages[request.url.path], request=request)

    def configure(self):
        return configure_monitoring(self.store, self.source["id"], source_version(self.source), 0, 3600)

    def monitor(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.company_source_monitoring)).mappings().one())


class CompanyMonitoringRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.close()

    def _scheduled(self, at=None):
        return schedule_company_sources(self.store, at=at)["scheduled"]

    def _execute(self, operation_id, fetch=True):
        with patch.dict(os.environ, self.fixture.env):
            if fetch:
                with patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch):
                    return execute_operation(operation_id)
            with patch.object(PublicCompanyClient, "get", side_effect=AssertionError("unexpected egress")):
                return execute_operation(operation_id)

    def test_cancelled_queued_operation_is_not_reused_or_reopened(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        cancelled = request_cancel(self.store, queued["operation_id"])
        self.assertEqual(cancelled["state"], "CANCELLED")
        tick = self._scheduled()
        self.assertEqual(tick, [])
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(m.operations.c.id == queued["operation_id"])).scalar_one()
        self.assertEqual(state, "CANCELLED")

    def test_next_due_tick_creates_new_operation_with_terminal_predecessor(self):
        self.fixture.configure()
        first = self._scheduled()[0]
        request_cancel(self.store, first["operation_id"])
        base = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(self._scheduled(at=base), [])
        second = self._scheduled(at=base + timedelta(seconds=3601))[0]
        self.assertNotEqual(second["operation_id"], first["operation_id"])
        with self.store.engine.connect() as connection:
            params = connection.execute(select(m.operations.c.params).where(m.operations.c.id == second["operation_id"])).scalar_one()
        self.assertEqual(params["predecessor_operation_id"], first["operation_id"])

    def test_paused_profiles_block_before_any_public_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.search_profiles).values(enabled=False))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SEARCH_PAUSED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_endpoint_retarget_blocks_before_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                               .values(endpoint="https://employer.example/changed.json"))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SOURCE_CHANGED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_disable_while_queued_blocks_before_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                               .values(enabled=False))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SOURCE_DISABLED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_multiple_profiles_share_one_refresh_receipt_and_import(self):
        second = copy.deepcopy(PROFILE)
        second["id"] = "fixture-two"
        self.store.save_profile(second)
        save_search_profile(self.store, dict(id="all-two", profile_id="fixture-two", name="All sources", source_ids=[]))
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(m.operations.c.id == queued["operation_id"])).mappings().one()
        self.assertEqual(operation["params"]["profile_ids"], ["fixture", "fixture-two"])
        result = self._execute(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["completed_operation_id"], queued["operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.matches.c.profile_id).distinct()).scalars().all(), ["fixture", "fixture-two"])

    def test_profile_scope_change_after_final_get_blocks_without_import_or_receipt(self):
        second = copy.deepcopy(PROFILE)
        second["id"] = "fixture-two"
        self.store.save_profile(second)
        save_search_profile(self.store, dict(id="all-two", profile_id="fixture-two", name="All sources", source_ids=[]))
        self.fixture.configure()
        queued = self._scheduled()[0]
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def pause_second_then_import(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.search_profiles).where(m.search_profiles.c.profile_id == "fixture-two")
                                   .values(enabled=False))
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=pause_second_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SEARCH_PROFILE_CHANGED")
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertEqual(self.store.count("job_matches"), 0)
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_conflicting_verified_domain_after_final_get_blocks_without_import_or_receipt(self):
        record_company_domain(self.store, self.fixture.company, "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Independent employer record"])
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "other-company", "name": "Other Employer", "domain": ""})
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def add_conflicting_domain_then_import(*args, **kwargs):
            record_company_domain(self.store, "other-company", "employer.example",
                                  "VERIFIED_EXISTING_DOMAIN", ["Independent conflicting employer record"])
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=add_conflicting_domain_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_IDENTITY_REVIEW_REQUIRED")
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_domain_ambiguity_after_first_network_response_blocks_without_failed_quality(self):
        record_company_domain(self.store, self.fixture.company, "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Independent employer record"])
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "other-company", "name": "Other Employer", "domain": ""})
        calls = 0

        def fetch_then_conflict(client, url, deadline):
            nonlocal calls
            calls += 1
            response = self.fixture.fetch(url, deadline)
            if calls == 1:
                record_company_domain(self.store, "other-company", "employer.example",
                                      "VERIFIED_EXISTING_DOMAIN", ["Independent conflicting employer record"])
            return response

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", fetch_then_conflict):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_IDENTITY_REVIEW_REQUIRED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_failed_feed_has_no_success_receipt_and_retains_failed_observation(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        self.fixture.pages["/jobs.json"] = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
            {"id": "duplicate", "title": "One", "url": "https://employer.example/jobs/a"},
            {"id": "duplicate", "title": "Two", "url": "https://employer.example/jobs/b"}]})
        result = self._execute(queued["operation_id"])
        self.assertIn(result["status"], {"FAILED", "BLOCKED"})
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertFalse(connection.execute(select(m.source_quality_observations.c.succeeded)).scalar_one())
        monitor = self.fixture.monitor()
        self.assertIsNone(monitor["completed_operation_id"])

    def test_network_failure_sets_backoff_without_success_receipt(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "get", side_effect=HTTPStatusError(503)):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "HTTP_STATUS_ERROR")
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["status"], "FAILED")
        self.assertEqual(monitor["failure_count"], 1)
        self.assertIsNone(monitor["completed_operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertFalse(connection.execute(select(m.source_quality_observations.c.succeeded)).scalar_one())

    def test_running_cancellation_does_not_add_failure_or_quality_observation(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        cancelled = False

        def fetch_and_cancel(client, url, deadline):
            nonlocal cancelled
            if not cancelled:
                request_cancel(self.store, queued["operation_id"])
                cancelled = True
            return self.fixture.fetch(url, deadline)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", fetch_and_cancel):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "CANCELLED", result)
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["failure_count"], 0)
        self.assertIsNone(monitor["completed_operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_source_retargeted_after_last_get_fails_import_guard_without_receipt(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def retarget_then_import(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                                   .values(endpoint="https://employer.example/retargeted.json"))
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=retarget_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_FEED_SOURCE_MISMATCH")
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertEqual(self.store.count("canonical_jobs"), 0)


if __name__ == "__main__":
    unittest.main()
