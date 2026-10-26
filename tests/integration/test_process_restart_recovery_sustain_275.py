"""MJ-REQ-274: durable discovery survives a worker-process crash and replay."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.operations import recover_expired

_WORKER_CHILD = r"""
import json
import os
import sys
from unittest.mock import patch

from services.discovery.pipeline import discover as real_discover
from workers.tasks import execute_operation

operation_id, crash_after_effect = sys.argv[1], sys.argv[2] == "crash"

def discover_then_optionally_crash(*args, **kwargs):
    result = real_discover(*args, **kwargs)
    if crash_after_effect:
        # Simulate an abrupt worker death after production discovery committed
        # its durable rows but before execute_operation can mark the outbox row
        # complete. os._exit intentionally bypasses Python cleanup handlers.
        os._exit(73)
    return result

with patch("workers.tasks.discover", side_effect=discover_then_optionally_crash):
    print(json.dumps(execute_operation(operation_id), sort_keys=True))
"""


class ProcessRestartRecoverySustain275Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-restart-275-")
        self.addCleanup(self.directory.cleanup)
        self.database_url = f"sqlite:///{(Path(self.directory.name) / 'restart.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.addCleanup(self.store.engine.dispose)

    def _worker(self, operation_id, origin, *, crash):
        environment = {
            **{key: os.environ[key] for key in (
                "PATH", "SYSTEMROOT", "SystemDrive", "WINDIR", "TEMP", "TMP",
                "LOCALAPPDATA", "APPDATA", "PYTHONPATH",
            ) if key in os.environ},
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_OUTPUT_DIR": self.directory.name,
            "MJOBS_MOCK_ORIGIN": origin,
            "PYTHONUTF8": "1",
        }
        return subprocess.run(
            [sys.executable, "-c", _WORKER_CHILD, operation_id, "crash" if crash else "finish"],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def _durable_effects(self, store, profile_id):
        with store.engine.connect() as connection:
            matches = [dict(row) for row in connection.execute(select(
                m.matches.c.job_id, m.matches.c.profile_id,
                m.matches.c.profile_version, m.matches.c.score,
                m.matches.c.missing, m.matches.c.components,
            ).where(
                m.matches.c.profile_id == profile_id,
            ).order_by(m.matches.c.job_id)).mappings()]
            discoveries = [dict(row) for row in connection.execute(select(
                m.audit.c.id, m.audit.c.entity_id, m.audit.c.action,
                m.audit.c.payload, m.audit.c.timestamp,
            ).where(
                m.audit.c.action == "JOB_DISCOVERED",
            ).order_by(m.audit.c.id)).mappings()]
        return {"matches": matches, "job_discovered_events": discoveries}

    def test_crash_after_discovery_recovers_once_and_replay_does_not_add_attempt(self):
        with fixture_server() as origin:
            environment = patch.dict(os.environ, {
                "MJOBS_DATABASE_URL": self.database_url,
                "MJOBS_OPERATION_MODE": "FIXTURE",
                "MJOBS_OUTPUT_DIR": self.directory.name,
                "MJOBS_MOCK_ORIGIN": origin,
            })
            environment.start()
            try:
                with TestClient(create_app(
                        self.store, origin, self.directory.name, operation_mode="FIXTURE")) as client:
                    profile = {**PROFILE, "id": "restart-275-" + uuid4().hex}
                    response = client.put("/profiles", json=profile)
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = {"profile_id": profile["id"], "request_id": uuid4().hex}
                    response = client.post("/discovery", json=payload)
                    self.assertEqual(response.status_code, 202, response.text)
                    operation_id = response.json()["operation_id"]
                    self.assertEqual(client.get(f"/operations/{operation_id}").json()["state"], "QUEUED")

                # A separate interpreter claims the operation, performs actual
                # loopback-fixture discovery, persists its result, then dies
                # before the durable operation can be marked complete.
                self.store.engine.dispose()
                crashed = self._worker(operation_id, origin, crash=True)
                self.assertEqual(crashed.returncode, 73, crashed.stderr)

                reopened = Store(self.database_url)
                try:
                    with reopened.engine.connect() as connection:
                        interrupted = connection.execute(select(m.operations).where(
                            m.operations.c.id == operation_id)).mappings().one()
                    self.assertEqual((interrupted["state"], interrupted["attempt_count"]), ("RUNNING", 1))
                    effects_before_retry = self._durable_effects(reopened, profile["id"])
                    self.assertGreater(len(effects_before_retry["matches"]), 0,
                                       "crashed worker must have persisted discovery results")
                    self.assertGreater(len(effects_before_retry["job_discovered_events"]), 0,
                                       "crashed worker must have persisted discovery audit events")

                    # Advancing only the durable lease models the passage of
                    # the normal lease interval; recovery itself remains the
                    # production operation and preserves the stable operation ID.
                    with reopened.engine.begin() as connection:
                        connection.execute(update(m.operations).where(
                            m.operations.c.id == operation_id).values(
                                lease_expires_at="2000-01-01T00:00:00+00:00"))
                    self.assertEqual(recover_expired(reopened), 1)
                    with reopened.engine.connect() as connection:
                        queued = connection.execute(select(
                            m.operations.c.state, m.operations.c.retry_count).where(
                                m.operations.c.id == operation_id)).one()
                    self.assertEqual((queued.state, queued.retry_count), ("QUEUED", 1))
                finally:
                    reopened.engine.dispose()

                # A fresh worker process redelivers the same operation. The
                # production discovery path must deduplicate its persisted
                # results while completing the operation.
                resumed = self._worker(operation_id, origin, crash=False)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout.splitlines()[-1])["status"], "COMPLETED")

                final_store = Store(self.database_url)
                try:
                    with TestClient(create_app(
                            final_store, origin, self.directory.name,
                            operation_mode="FIXTURE")) as restarted_client:
                        final = restarted_client.get(f"/operations/{operation_id}").json()
                        self.assertEqual((final["state"], final["attempt_count"]), ("COMPLETED", 2))
                        replay = restarted_client.post("/discovery", json=payload)
                        self.assertEqual(replay.status_code, 202, replay.text)
                        self.assertEqual(replay.json()["operation_id"], operation_id)
                        self.assertEqual(replay.json()["state"], "COMPLETED")
                    self.assertEqual(self._durable_effects(final_store, profile["id"]), effects_before_retry)

                    # A stale duplicate worker delivery after completion is
                    # inert in a third fresh interpreter: no extra attempt or
                    # duplicate durable discovery effect is permitted.
                    duplicate = self._worker(operation_id, origin, crash=False)
                    self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                    self.assertEqual(
                        json.loads(duplicate.stdout.splitlines()[-1])["status"],
                        "ALREADY_CLAIMED_OR_CAPACITY",
                    )
                    with final_store.engine.connect() as connection:
                        persisted = connection.execute(select(
                            m.operations.c.state, m.operations.c.attempt_count,
                            m.operations.c.retry_count).where(
                                m.operations.c.id == operation_id)).one()
                    self.assertEqual(
                        (persisted.state, persisted.attempt_count, persisted.retry_count),
                        ("COMPLETED", 2, 1),
                    )
                    self.assertEqual(self._durable_effects(final_store, profile["id"]), effects_before_retry)
                finally:
                    final_store.engine.dispose()
            finally:
                environment.stop()


if __name__ == "__main__":
    unittest.main()
