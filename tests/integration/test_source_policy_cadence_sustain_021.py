"""Connected sustain proof for MJ-REQ-020 source policy and cadence.

The test uses HTTPX's in-process transport only.  It exercises the production
source configuration, collection state transitions, durable Store reopen, and
scheduler/outbox boundary without contacting a provider or Redis service.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import scheduler_tick
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.greenhouse import SourceError
from tests.integration.test_spine import PROFILE


class HeartbeatRecorder:
    def __init__(self):
        self.records = []

    def set(self, key, value, ex):
        self.records.append((key, json.loads(value), ex))


def _as_utc(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SourcePolicyCadenceSustainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-source-policy-")
        self.addCleanup(self.directory.cleanup)
        database = Path(self.directory.name) / "policy.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.addCleanup(self.store.engine.dispose)

    @staticmethod
    def _healthy_transport(request):
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(200, json={"name": "Synthetic Healthy Board"})

    @staticmethod
    def _rate_limited_transport(_request):
        return httpx.Response(429, headers={"retry-after": "600"})

    def test_policy_survives_reopen_and_failure_cannot_block_independent_due_source(self):
        healthy = configure_source(
            self.store,
            "cadence-healthy",
            enabled=True,
            poll_interval=300,
            concurrency=2,
            request_timeout=9,
            rate_limit=45,
            jitter_seconds=0,
        )
        failing = configure_source(
            self.store,
            "cadence-failing",
            enabled=True,
            poll_interval=300,
            concurrency=3,
            request_timeout=8,
            rate_limit=30,
            jitter_seconds=0,
        )
        disabled = configure_source(
            self.store,
            "cadence-disabled",
            enabled=False,
            poll_interval=900,
            concurrency=4,
            request_timeout=7,
            rate_limit=20,
            jitter_seconds=0,
        )
        save_search_profile(
            self.store,
            {
                "id": "source-policy-sustain",
                "profile_id": PROFILE["id"],
                "name": "Source policy sustain",
                "enabled": True,
                "include_titles": ["Engineer"],
                "remote": "ANY",
                "employment_types": [],
                "source_ids": [healthy, failing, disabled],
            },
        )

        requested = []

        def fail(request):
            requested.append(("failing", str(request.url)))
            return self._rate_limited_transport(request)

        def succeed(request):
            requested.append(("healthy", str(request.url)))
            return self._healthy_transport(request)

        with self.assertRaisesRegex(SourceError, "RATE_LIMITED"):
            collect_source(
                self.store,
                failing,
                PROFILE["id"],
                transport=httpx.MockTransport(fail),
            )
        healthy_result = collect_source(
            self.store,
            healthy,
            PROFILE["id"],
            transport=httpx.MockTransport(succeed),
        )
        self.assertEqual(healthy_result["jobs_seen"], 0)
        self.assertEqual({kind for kind, _ in requested}, {"failing", "healthy"})
        self.assertEqual(
            {urlsplit(url).hostname for _, url in requested},
            {"boards-api.greenhouse.io"},
        )

        self.store.engine.dispose()
        reopened = Store(self.store.url)
        self.addCleanup(reopened.engine.dispose)
        with reopened.engine.connect() as connection:
            rows = {
                row["id"]: dict(row)
                for row in connection.execute(select(m.job_sources)).mappings()
            }

        healthy_row = rows[healthy]
        failing_row = rows[failing]
        disabled_row = rows[disabled]
        self.assertEqual(
            {
                "enabled": healthy_row["enabled"],
                "poll_interval": healthy_row["poll_interval"],
                "concurrency": healthy_row["concurrency"],
                "request_timeout": healthy_row["request_timeout"],
                "rate_limit": healthy_row["rate_limit"],
                "collection_strategy": healthy_row["strategy"],
                "consecutive_failures": healthy_row["consecutive_failures"],
                "backoff_until": healthy_row["backoff_until"],
                "health_status": healthy_row["health_status"],
            },
            {
                "enabled": True,
                "poll_interval": 300,
                "concurrency": 2,
                "request_timeout": 9,
                "rate_limit": 45,
                "collection_strategy": "API",
                "consecutive_failures": 0,
                "backoff_until": None,
                "health_status": "HEALTHY",
            },
        )
        self.assertIsNotNone(healthy_row["last_attempt"])
        self.assertIsNotNone(healthy_row["last_success"])
        self.assertGreaterEqual(
            _as_utc(healthy_row["last_success"]),
            _as_utc(healthy_row["last_attempt"]),
        )
        self.assertEqual(
            {
                "enabled": failing_row["enabled"],
                "poll_interval": failing_row["poll_interval"],
                "concurrency": failing_row["concurrency"],
                "request_timeout": failing_row["request_timeout"],
                "rate_limit": failing_row["rate_limit"],
                "collection_strategy": failing_row["strategy"],
                "last_success": failing_row["last_success"],
                "consecutive_failures": failing_row["consecutive_failures"],
                "health_status": failing_row["health_status"],
            },
            {
                "enabled": True,
                "poll_interval": 300,
                "concurrency": 3,
                "request_timeout": 8,
                "rate_limit": 30,
                "collection_strategy": "API",
                "last_success": None,
                "consecutive_failures": 1,
                "health_status": "RATE_LIMITED",
            },
        )
        self.assertIsNotNone(failing_row["last_attempt"])
        self.assertIsNotNone(failing_row["backoff_until"])
        self.assertGreater(
            _as_utc(failing_row["backoff_until"]),
            _as_utc(failing_row["last_attempt"]) + timedelta(seconds=300),
        )
        self.assertEqual(
            (
                disabled_row["enabled"],
                disabled_row["poll_interval"],
                disabled_row["concurrency"],
                disabled_row["request_timeout"],
                disabled_row["rate_limit"],
                disabled_row["strategy"],
                disabled_row["last_attempt"],
                disabled_row["last_success"],
                disabled_row["consecutive_failures"],
                disabled_row["backoff_until"],
                disabled_row["health_status"],
            ),
            (False, 900, 4, 7, 20, "API", None, None, 0, None, "NOT_CHECKED"),
        )

        healthy_due = _as_utc(healthy_row["last_attempt"]) + timedelta(seconds=300)
        failing_due = _as_utc(failing_row["backoff_until"])
        recorder = HeartbeatRecorder()

        early = scheduler_tick(reopened, recorder, at=healthy_due - timedelta(microseconds=1))
        self.assertEqual(early["scheduled"], [])
        self.assertEqual(
            {row["source_id"]: row["reason"] for row in early["skipped"]},
            {healthy: "NOT_DUE", failing: "NOT_DUE"},
        )

        healthy_tick = scheduler_tick(reopened, recorder, at=healthy_due)
        self.assertEqual([row["source_id"] for row in healthy_tick["scheduled"]], [healthy])
        self.assertEqual(
            {row["source_id"]: row["reason"] for row in healthy_tick["skipped"]},
            {failing: "NOT_DUE"},
        )
        self.assertNotIn(disabled, {
            row["source_id"] for row in healthy_tick["scheduled"] + healthy_tick["skipped"]
        })

        operation_id = healthy_tick["scheduled"][0]["operation_id"]
        with reopened.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id
            )).mappings().one())
        self.assertEqual(operation["state"], "QUEUED")
        self.assertEqual(operation["kind"], "DISCOVER")
        self.assertEqual(operation["params"]["source_id"], healthy)
        self.assertEqual(operation["params"]["profile_ids"], [PROFILE["id"]])

        before_backoff = scheduler_tick(
            reopened,
            recorder,
            at=failing_due - timedelta(microseconds=1),
        )
        self.assertIn(
            {"source_id": failing, "reason": "NOT_DUE", "due_at": failing_due.isoformat()},
            before_backoff["skipped"],
        )
        with reopened.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id
            ).values(state="COMPLETED"))
        at_backoff = scheduler_tick(reopened, recorder, at=failing_due)
        self.assertIn(failing, {row["source_id"] for row in at_backoff["scheduled"]})
        self.assertTrue(all(record[0] == "mjobs:runtime:scheduler:last_success" for record in recorder.records))
        self.assertEqual(recorder.records[-1][1]["timestamp"], failing_due.isoformat())

        final_store = Store(self.store.url)
        try:
            with final_store.engine.connect() as connection:
                queued_sources = {
                    row["params"]["source_id"]
                    for row in connection.execute(select(m.operations)).mappings()
                }
            self.assertIn(healthy, queued_sources)
            self.assertIn(failing, queued_sources)
            self.assertNotIn(disabled, queued_sources)
        finally:
            final_store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
