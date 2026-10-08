"""Source capability, policy, search-profile and scheduler acceptance."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_sources
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from services.discovery.sources.contract import CollectionStrategy, SourceCapability
from services.discovery.sources.registry import source_adapter
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class SourcePolicySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def second_profile(self):
        profile = {
            **PROFILE,
            "id": "fixture-two",
            "email": "second@example.test",
            "facts": [dict(row) for row in PROFILE["facts"]],
            "answers": [dict(row) for row in PROFILE.get("answers", [])],
        }
        self.store.save_profile(profile)
        return profile["id"]

    def test_source_declares_typed_capabilities_and_persists_complete_policy(self):
        source_id = configure_source(
            self.store, "example", poll_interval=1800, concurrency=2,
            request_timeout=12, rate_limit=30, jitter_seconds=17,
        )
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one())
        capabilities = source_adapter(source).capabilities()
        self.assertEqual(capabilities.strategy, CollectionStrategy.API)
        self.assertTrue(capabilities.supports(SourceCapability.DISCOVERY))
        self.assertTrue(capabilities.supports(SourceCapability.SALARY))
        self.assertTrue(capabilities.supports(SourceCapability.APPLICATION_REDIRECT))
        self.assertFalse(capabilities.supports(SourceCapability.DIRECT_APPLICATION))
        self.assertEqual(source["capabilities"], capabilities.as_dict())
        self.assertEqual(
            (source["poll_interval"], source["concurrency"], source["request_timeout"],
             source["rate_limit"], source["jitter_seconds"]),
            (1800, 2, 12, 30, 17),
        )
        adapter = source_adapter(source)
        self.assertEqual((adapter.request_timeout, adapter.rate_limit, adapter.concurrency), (12, 30, 2))

    def test_search_profiles_are_data_driven_persistent_and_source_scoped(self):
        first = configure_source(self.store, "first")
        second = configure_source(self.store, "second")
        with TestClient(create_app(
                self.store, "http://127.0.0.1:8090", self.root,
                "redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
            response = client.put("/search-profiles", json={
                "id": "infra", "profile_id": "fixture", "name": "Remote Infrastructure",
                "include_titles": ["System Administrator", "system administrator", "NOC Engineer"],
                "exclude_titles": ["Senior"],
                "required_keywords": ["Active Directory", "Microsoft 365"],
                "optional_keywords": ["Azure"], "excluded_keywords": ["commission"],
                "exact_phrases": ["Group Policy"], "technology_aliases": {"Microsoft 365": ["M365"]},
                "required_skills": ["M365", "Active Directory"], "optional_skills": ["Rubrik"], "excluded_skills": ["AWS"],
                "company_include": ["Example"], "company_exclude": ["Staffing"],
                "remote": "REQUIRED", "work_arrangements": ["REMOTE"],
                "countries": ["US"], "states": ["TX"], "cities": ["Sherman"],
                "remote_regions": ["TX", "OK"], "employment_types": ["FULL_TIME"],
                "experience_levels": ["MID", "SENIOR"], "salary_min": 90000, "salary_max": 180000,
                "salary_period": "YEAR", "salary_known_only": True, "salary_unknown_allowed": False,
                "date_window": "CUSTOM", "date_start": "2026-09-01T00:00:00Z", "date_end": "2026-10-31T23:59:59Z",
                "clearance_levels": ["NONE_STATED", "PUBLIC_TRUST"], "exclude_clearance_required": False,
                "excluded_work_authorization_flags": ["NO_SPONSORSHIP", "US_CITIZENSHIP_REQUIRED"],
                "excluded_conditions": ["ON_CALL", "NIGHT_SHIFT", "COMMISSION"], "max_travel_percent": 25,
                "source_ids": [first],
            })
            self.assertEqual(response.status_code, 200)
            client.put("/search-profiles", json={
                "id": "support", "profile_id": "fixture", "name": "Support",
                "include_titles": ["Technical Support Engineer"], "source_ids": [second],
            }).raise_for_status()
            rows = client.get("/search-profiles", params={"profile_id": "fixture"}).json()["search_profiles"]
        self.assertEqual(len(rows), 2)
        infra = next(row for row in rows if row["id"] == "infra")
        self.assertEqual(infra["criteria"]["include_titles"], ["System Administrator", "NOC Engineer"])
        self.assertEqual(infra["criteria"]["remote"], "REQUIRED")
        self.assertEqual(infra["criteria"]["exclude_titles"], ["Senior"])
        self.assertEqual(infra["criteria"]["required_keywords"], ["Active Directory", "Microsoft 365"])
        self.assertEqual(infra["criteria"]["technology_aliases"], {"Microsoft 365": ["M365"]})
        self.assertEqual(infra["criteria"]["required_skills"], ["MICROSOFT_365", "ACTIVE_DIRECTORY"])
        self.assertEqual(infra["criteria"]["optional_skills"], ["RUBRIK"])
        self.assertEqual(infra["criteria"]["excluded_skills"], ["AWS"])
        self.assertEqual(infra["criteria"]["work_arrangements"], ["REMOTE"])
        self.assertEqual(infra["criteria"]["remote_regions"], ["TX", "OK"])
        self.assertEqual(infra["criteria"]["experience_levels"], ["MID", "SENIOR"])
        self.assertEqual((infra["criteria"]["salary_min"], infra["criteria"]["salary_max"], infra["criteria"]["salary_period"]), (90000.0, 180000.0, "YEAR"))
        self.assertEqual(infra["criteria"]["date_window"], "CUSTOM")
        self.assertTrue(infra["criteria"]["date_start"].endswith("+00:00"))
        self.assertEqual(infra["criteria"]["clearance_levels"], ["NONE_STATED", "PUBLIC_TRUST"])
        self.assertEqual(infra["criteria"]["excluded_work_authorization_flags"], ["NO_SPONSORSHIP", "US_CITIZENSHIP_REQUIRED"])
        self.assertEqual(infra["criteria"]["excluded_conditions"], ["ON_CALL", "NIGHT_SHIFT", "COMMISSION"])
        self.assertEqual(infra["criteria"]["max_travel_percent"], 25.0)
        self.assertEqual(infra["source_ids"], [first])
        self.assertEqual(self.store.count("search_profiles"), 2)

    def test_scheduler_respects_independent_source_cadence_and_deduplicates_ticks(self):
        second_profile = self.second_profile()
        fast = configure_source(self.store, "fast", poll_interval=900, jitter_seconds=0)
        slow = configure_source(self.store, "slow", poll_interval=1800, jitter_seconds=0)
        for identifier, profile_id in (("one", "fixture"), ("two", second_profile)):
            save_search_profile(self.store, {
                "id": identifier, "profile_id": profile_id, "name": identifier,
                "enabled": True, "include_titles": ["Engineer"], "remote": "ANY",
                "employment_types": [], "source_ids": [],
            })
        at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        first_tick = schedule_due_sources(self.store, at)
        self.assertEqual({row["source_id"] for row in first_tick["scheduled"]}, {fast, slow})
        self.assertTrue(all(row["profile_ids"] == ["fixture", "fixture-two"] for row in first_tick["scheduled"]))
        self.assertEqual(schedule_due_sources(self.store, at)["scheduled"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).values(state="COMPLETED"))
            connection.execute(update(m.job_sources).values(last_attempt=at.isoformat()))
        later = schedule_due_sources(self.store, at + timedelta(minutes=15))
        self.assertEqual([row["source_id"] for row in later["scheduled"]], [fast])
        reasons = {row["source_id"]: row["reason"] for row in later["skipped"]}
        self.assertEqual(reasons[slow], "NOT_DUE")

    def test_scheduled_collection_fetches_once_then_rescores_other_profiles(self):
        second_profile = self.second_profile()
        source_id = configure_source(self.store, "scheduled", jitter_seconds=0)
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "source_id": source_id, "profile_ids": ["fixture", second_profile],
            "scheduled_for": "initial", "scheduler": "source-policy-v1",
        })
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "PERSONAL",
        }
        with patch.dict("os.environ", environment), \
                patch("workers.tasks.collect_source") as collect, \
                patch("workers.tasks.recalculate") as recalculate:
            result = execute_operation(operation)
        self.assertEqual(result["status"], "COMPLETED")
        collect.assert_called_once()
        # The worker opens its own Store; verify the source/profile arguments.
        self.assertEqual(collect.call_args.args[1:3], (source_id, "fixture"))
        recalculate.assert_called_once()
        self.assertEqual(recalculate.call_args.args[1], second_profile)

    def test_due_discovery_survives_store_restart_and_completes_once(self):
        source_id = configure_source(self.store, "restart-synthetic", jitter_seconds=0)
        save_search_profile(self.store, {
            "id": "restart-synthetic", "profile_id": "fixture", "name": "Restart synthetic",
            "enabled": True, "include_titles": ["Engineer"], "remote": "ANY",
            "employment_types": [], "source_ids": [source_id],
        })
        self.store.engine.dispose()

        scheduler_once = """
import json
import os
from pathlib import Path
from services.discovery import scheduler
schedule_due_sources = scheduler.schedule_due_sources
def capture_tick(*args, **kwargs):
    result = schedule_due_sources(*args, **kwargs)
    Path(os.environ["MJOBS_SCHEDULER_TICK_REPORT"]).write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8")
    return result
scheduler.schedule_due_sources = capture_tick
def stop_after_tick(_seconds):
    raise SystemExit(0)
scheduler.time.sleep = stop_after_tick
scheduler.run_forever()
"""
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update({
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_SCHEDULER_TICK_SECONDS": "5",
        })
        project_root = Path(__file__).resolve().parents[2]

        def run_scheduler_process(name):
            report_path = self.root / f"{name}-tick.json"
            report_path.unlink(missing_ok=True)
            process_environment = {**environment, "MJOBS_SCHEDULER_TICK_REPORT": str(report_path)}
            completed = subprocess.run(
                [sys.executable, "-c", scheduler_once], cwd=project_root, env=process_environment,
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(report_path.is_file(), "scheduler process did not execute a tick")
            return json.loads(report_path.read_text(encoding="utf-8"))

        first_tick = run_scheduler_process("scheduler-first")
        restarted = Store(self.store.url)
        try:
            self.assertEqual(len(first_tick["scheduled"]), 1)
            self.assertEqual(first_tick["scheduled"][0]["source_id"], source_id)
            with restarted.engine.connect() as connection:
                operations = list(connection.execute(select(m.operations)).mappings())
            self.assertEqual(len(operations), 1, "first scheduler restart must persist one operation")
            operation_id = operations[0]["id"]
            self.assertEqual(operations[0]["state"], "QUEUED")
            self.assertEqual(operations[0]["params"]["source_id"], source_id)

            restarted.engine.dispose()
            second_tick = run_scheduler_process("scheduler-restarted")
            self.assertEqual(second_tick["scheduled"], [])
            self.assertIn({"source_id": source_id, "reason": "ACTIVE_OPERATION"}, second_tick["skipped"])
            with restarted.engine.connect() as connection:
                self.assertEqual(connection.execute(select(m.operations.c.id)).all(), [(operation_id,)])

            worker_once = """
import json
import os
from pathlib import Path
from sqlalchemy import update
from packages.persistence import models as m
from packages.persistence.store import now
from workers import tasks
def synthetic_collect(store, source_id, profile_id, profile_version=None, check=None, transport=None):
    if check:
        check()
    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            last_attempt=now(), health_status="HEALTHY", consecutive_failures=0,
            last_error=None, backoff_until=None))
    return {"status": "SYNTHETIC_NO_NETWORK"}
tasks.collect_source = synthetic_collect
result = tasks.execute_operation(os.environ["MJOBS_TEST_OPERATION_ID"])
Path(os.environ["MJOBS_WORKER_REPORT"]).write_text(
    json.dumps(result, sort_keys=True), encoding="utf-8")
"""
            worker_report = self.root / "worker-result.json"
            worker_environment = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
                if key in os.environ
            }
            worker_environment.update({
                "MJOBS_DATABASE_URL": self.store.url,
                "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
                "MJOBS_OUTPUT_DIR": str(self.root),
                "MJOBS_OPERATION_MODE": "PERSONAL",
                "MJOBS_TEST_OPERATION_ID": operation_id,
                "MJOBS_WORKER_REPORT": str(worker_report),
            })
            worker = subprocess.run(
                [sys.executable, "-c", worker_once], cwd=project_root, env=worker_environment,
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertTrue(worker_report.is_file(), "worker process did not complete the operation")
            worker_result = json.loads(worker_report.read_text(encoding="utf-8"))
            self.assertEqual(worker_result["status"], "COMPLETED")

            final_tick = run_scheduler_process("scheduler-after-worker-restart")
            self.assertEqual(final_tick["scheduled"], [])
            self.assertTrue(any(row["source_id"] == source_id and row["reason"] == "NOT_DUE"
                                for row in final_tick["skipped"]))
            with restarted.engine.connect() as connection:
                completed = connection.execute(select(m.operations).where(
                    m.operations.c.id == operation_id)).mappings().one()
                self.assertEqual(connection.execute(select(m.operations.c.id)).all(), [(operation_id,)])
            self.assertEqual(completed["state"], "COMPLETED")
        finally:
            restarted.engine.dispose()

    def test_compose_recovery_restarts_scheduler_service(self):
        from cli.compose_recovery import RECOVERY_RESTART_SERVICES, restart_recovery_services

        with patch("cli.compose_recovery.compose") as compose:
            restart_recovery_services()

        self.assertIn("scheduler", RECOVERY_RESTART_SERVICES)
        compose.assert_called_once_with("restart", *RECOVERY_RESTART_SERVICES)


if __name__ == "__main__":
    unittest.main()
