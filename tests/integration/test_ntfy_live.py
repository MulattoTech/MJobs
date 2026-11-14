"""Explicit owned-loopback official ntfy smoke; never contacts a public topic."""
import ipaddress
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from packages.persistence.store import Store, now
from packages.persistence import models as m
from services.notifications.configuration import parse_config
from services.notifications.journal import DeliveryJournal
from services.notifications.providers.ntfy import NtfyProvider
from services.notifications.runner import run_once


def _owned_local_origin(raw):
    try:
        parsed = urlsplit(raw)
        address = ipaddress.ip_address(parsed.hostname or "")
        valid = (
            parsed.scheme == "http" and address.is_loopback and parsed.port is not None
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and parsed.path in ("", "/")
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Explicit literal-loopback owned ntfy URL required")
    return raw.rstrip("/")


@pytest.mark.skipif(
    os.environ.get("MJOBS_ENABLE_NTFY_LIVE") != "1",
    reason="NOT_RUN: explicit owned local ntfy smoke not enabled",
)
def test_official_local_ntfy_delivery_and_restart_deduplication(tmp_path):
    origin = _owned_local_origin(os.environ.get("MJOBS_NTFY_LIVE_URL", ""))
    topic = "mjobs-fixture-" + uuid4().hex
    config = parse_config({
        "version": 1, "enabled": True, "provider": "ntfy",
        "profile_id": "synthetic-notification", "server_url": origin,
        "topic": topic, "state_path": str(tmp_path / "delivery.sqlite3"),
        "allow_loopback_http": True, "request_timeout": 5,
    })
    store_url = f"sqlite:///{(tmp_path / 'synthetic.db').as_posix()}"
    store = Store(store_url)
    store.migrate()
    store.save_profile({"id": config.profile_id, "name": "Synthetic Local Fixture",
                        "email": "synthetic@example.test", "facts": [], "answers": []})
    journal = DeliveryJournal(config)
    try:
        with store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id="synthetic-operation", kind="DISCOVER", entity_id=config.profile_id,
                params={}, state="COMPLETED", updated_at=now(), completed_at=now(), attempt_count=1,
            ))
        sender = NtfyProvider(config)
        first = run_once(store, config, journal, sender)
        assert first["attempted"] == 1
        assert first["outcome"] == "NOTIFICATION_SERVER_ACKNOWLEDGED"
        receipt = journal.status()["deliveries"][0]
        assert receipt["status"] == "DELIVERED"
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False,
                          headers={"Accept-Encoding": "identity"}) as client:
            response = client.get(origin + "/" + topic + "/json", params={"poll": "1", "since": "all"})
            assert response.status_code == 200
            messages = [json.loads(line) for line in response.text.splitlines() if line.strip()]
            observed = [row for row in messages if row.get("event") == "message"]
            assert len(observed) == 1
            assert observed[0]["id"] == receipt["message_id"]
            assert observed[0]["message"] == "Job discovery completed. Open MJobs for details."
            assert "Synthetic Local Fixture" not in response.text
            assert "synthetic@example.test" not in response.text
            store.engine.dispose()
            store = Store(store_url)
            restarted = DeliveryJournal(config)
            assert run_once(store, config, restarted, NtfyProvider(config))["attempted"] == 0
            again = client.get(origin + "/" + topic + "/json", params={"poll": "1", "since": "all"})
            assert again.status_code == 200
            observed_again = [__import__("json").loads(line) for line in again.text.splitlines() if line.strip()]
            assert len([row for row in observed_again if row.get("event") == "message"]) == 1
    finally:
        store.engine.dispose()
