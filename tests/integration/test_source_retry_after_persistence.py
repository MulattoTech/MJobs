"""Provider cooldowns survive reopen and gate actual scheduler/outbox admission."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import scheduler_tick
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError
from services.discovery.sources import retry_after
from tests.integration.test_source_policy_cadence_sustain_021 import HeartbeatRecorder
from tests.integration.test_spine import PROFILE
from workers.retries import discovery_retry_plan


INSTANT = datetime(2030, 1, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)


class ReceiptClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return INSTANT.astimezone(tz) if tz else INSTANT.replace(tzinfo=None)


class SourceRetryAfterPersistenceTests(unittest.TestCase):
    def exercise_cooldown(self, provider, status, *, network_primary=False, header=None):
        collection = importlib.import_module("services.discovery.sources.collect")
        requested = (INSTANT + timedelta(hours=2)).replace(microsecond=0)
        header = format_datetime(requested, usegmt=True) if header is None else header
        with tempfile.TemporaryDirectory(prefix="mjobs-retry-after-") as root:
            url = f"sqlite:///{(Path(root) / 'cooldown.db').as_posix()}"
            store = Store(url)
            reopened = None
            try:
                store.migrate()
                store.bind_workspace_mode("PERSONAL")
                store.save_profile(PROFILE)
                failing = configure_source(store, "synthetic-failing", provider=provider, jitter_seconds=0)
                healthy = configure_source(store, "synthetic-healthy", jitter_seconds=0)
                save_search_profile(store, {
                    "id": "cooldown-search", "profile_id": PROFILE["id"], "name": "Cooldown fixture",
                    "enabled": True, "include_titles": ["Engineer"], "remote": "ANY",
                    "employment_types": [], "source_ids": [failing, healthy],
                })
                calls = []

                def response(request):
                    calls.append(str(request.url))
                    if network_primary and not request.url.path.endswith("/jobs"):
                        raise httpx.ReadTimeout("synthetic-private-transport-detail", request=request)
                    return httpx.Response(status, headers={"Retry-After": header})

                with patch.object(retry_after, "datetime", ReceiptClock), \
                        patch.object(collection, "datetime", ReceiptClock), \
                        self.assertRaises(SourceError) as failed:
                    collect_source(store, failing, PROFILE["id"], transport=httpx.MockTransport(response))
                self.assertEqual(len(calls), 2 if provider == "greenhouse" else 1)
                self.assertEqual(len(set(calls)), len(calls))
                self.assertNotIn("synthetic-private", str(failed.exception))
                if network_primary:
                    self.assertEqual(failed.exception.code, "SOURCE_NETWORK_ERROR")

                store.engine.dispose()
                reopened = Store(url)
                with reopened.engine.connect() as connection:
                    source = dict(connection.execute(select(m.job_sources).where(
                        m.job_sources.c.id == failing)).mappings().one())
                    due = datetime.fromisoformat(source["backoff_until"])
                    plan = discovery_retry_plan(connection, {
                        "kind": "DISCOVER", "params": {"source_id": failing}, "attempt_count": 1,
                    }, failed.exception, INSTANT.isoformat())
                    self.assertEqual(connection.execute(select(m.jobs)).all(), [])
                    self.assertEqual(connection.execute(select(m.postings)).all(), [])
                    self.assertNotIn(header, json.dumps(source, default=str))
                if failed.exception.retry_seconds > retry_after.MAX_RETRY_SECONDS:
                    self.assertFalse(source["enabled"])
                    self.assertIsNone(plan)
                    self.assertEqual(due, datetime.max.replace(tzinfo=timezone.utc))
                    tick = scheduler_tick(reopened, HeartbeatRecorder(), at=INSTANT + timedelta(days=366))
                    self.assertNotIn(failing, {row["source_id"] for row in tick["scheduled"]})
                    return

                self.assertTrue(source["enabled"])
                self.assertEqual(failed.exception.retry_seconds, 7200)
                self.assertGreaterEqual(due, requested)
                self.assertGreaterEqual(datetime.fromisoformat(plan["next_attempt_at"]), requested)
                recorder = HeartbeatRecorder()
                early = scheduler_tick(reopened, recorder, at=due - timedelta(microseconds=1))
                self.assertNotIn(failing, {row["source_id"] for row in early["scheduled"]})
                self.assertIn({"source_id": failing, "reason": "NOT_DUE", "due_at": due.isoformat()},
                              early["skipped"])
                self.assertIn(healthy, {row["source_id"] for row in early["scheduled"]})
                with reopened.engine.begin() as connection:
                    operations = list(connection.execute(select(m.operations)).mappings())
                    self.assertEqual({row["params"]["source_id"] for row in operations}, {healthy})
                    connection.execute(update(m.operations).values(state="COMPLETED"))
                ready = scheduler_tick(reopened, recorder, at=due)
                self.assertIn(failing, {row["source_id"] for row in ready["scheduled"]})
                with reopened.engine.connect() as connection:
                    sources = {row["params"]["source_id"] for row in
                               connection.execute(select(m.operations)).mappings()}
                    self.assertEqual(sources, {healthy, failing})
            finally:
                if reopened:
                    reopened.engine.dispose()
                store.engine.dispose()

    def test_date_cooldowns_persist_and_do_not_starve_independent_sources(self):
        for provider in ("remotive", "remoteok", "wwr", "greenhouse"):
            for status in (429, 503):
                with self.subTest(provider=provider, status=status):
                    self.exercise_cooldown(provider, status)

    def test_mixed_network_primary_keeps_the_other_responses_durable_cooldown(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.exercise_cooldown("greenhouse", status, network_primary=True, header="7200")

    def test_extreme_or_oversized_headers_disable_automatic_collection(self):
        for provider in ("remotive", "remoteok", "wwr", "greenhouse"):
            for header in ("9" * 128, "9" * 10000):
                with self.subTest(provider=provider, length=len(header)):
                    self.exercise_cooldown(provider, 429, header=header)
