"""Private durable delivery reservations. No uncertain send is automatically replayed."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from services.notifications.contract import NotificationError, NotificationEvent


_RETRYABLE = {
    "NOTIFICATION_CREDENTIAL_UNAVAILABLE", "NOTIFICATION_AUTH_REJECTED",
    "NOTIFICATION_RATE_LIMITED", "NOTIFICATION_NOT_CONNECTED",
}
_MAX_ATTEMPTS = 3


class DeliveryJournal:
    def __init__(self, config, *, clock=time.time):
        self.path = Path(config.state_path)
        self.binding = config.binding
        self.clock = clock
        if self.path.is_symlink():
            raise NotificationError("NOTIFICATION_STATE_UNSAFE")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                code TEXT NOT NULL, message_id TEXT,
                claimed_at REAL, token TEXT, not_before REAL NOT NULL DEFAULT 0,
                recorded_at REAL NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(status, recorded_at)")
            values = dict(connection.execute("SELECT key, value FROM metadata"))
            if values:
                if values.get("schema_version") != "1" or values.get("binding") != self.binding:
                    raise NotificationError("NOTIFICATION_STATE_BINDING_MISMATCH")
                if not {"activated_at", "scan_cursor", "provider_not_before", "last_tick"} <= values.keys():
                    raise NotificationError("NOTIFICATION_STATE_INVALID")
            else:
                stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
                values = {
                    "schema_version": "1", "binding": self.binding,
                    "activated_at": stamp, "scan_cursor": "",
                    "provider_not_before": "0", "last_tick": "0",
                }
                connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", values.items())
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _metadata(connection):
        return {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM metadata")}

    def scan_position(self):
        with self._connection() as connection:
            values = self._metadata(connection)
        return values["activated_at"], values["scan_cursor"]

    def enqueue_page(self, events, *, expected_cursor, next_cursor):
        with self._transaction() as connection:
            current = self._metadata(connection)
            if current["scan_cursor"] != expected_cursor:
                return 0
            inserted = 0
            for event in events:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO deliveries
                       (event_key,payload,status,code,recorded_at) VALUES (?,?,'PENDING','NOTIFICATION_QUEUED',?)""",
                    (event.event_key, json.dumps(asdict(event), separators=(",", ":")), self.clock()),
                )
                inserted += cursor.rowcount
            connection.execute("UPDATE metadata SET value=? WHERE key='scan_cursor'", (next_cursor,))
            return inserted

    def claim_next(self):
        with self._transaction() as connection:
            values = self._metadata(connection)
            current_time = self.clock()
            if float(values["provider_not_before"]) > current_time:
                return None
            row = connection.execute(
                """SELECT * FROM deliveries WHERE status='PENDING' AND not_before<=?
                   AND attempts<? ORDER BY recorded_at,event_key LIMIT 1""",
                (current_time, _MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            connection.execute(
                """UPDATE deliveries SET status='SENDING',attempts=attempts+1,
                   token=?,claimed_at=?,code='NOTIFICATION_IN_FLIGHT'
                   WHERE event_key=? AND status='PENDING'""",
                (token, current_time, row["event_key"]),
            )
            # A durable gap spaces reservations across concurrent workers. Scheduling
            # after this transaction can still bunch actual socket writes.
            connection.execute("UPDATE metadata SET value=? WHERE key='provider_not_before'",
                               (str(current_time + 1),))
        try:
            event = NotificationEvent(**json.loads(row["payload"]))
        except (TypeError, ValueError):
            raise NotificationError("NOTIFICATION_STATE_INVALID") from None
        return event, token

    def finish(self, event_key, token, result):
        if result.status not in {"DELIVERED", "REJECTED", "UNKNOWN", "FAILED"}:
            raise NotificationError("NOTIFICATION_RESULT_INVALID")
        with self._transaction() as connection:
            current_time = self.clock()
            not_before = current_time + max(0, result.retry_after)
            changed = connection.execute(
                """UPDATE deliveries SET status=?,code=?,message_id=?,not_before=?
                   WHERE event_key=? AND status='SENDING' AND token=?""",
                (result.status, result.code, result.message_id, not_before, event_key, token),
            ).rowcount
            if changed != 1:
                raise NotificationError("NOTIFICATION_DELIVERY_OWNERSHIP_LOST")
            if result.retry_after:
                prior = float(self._metadata(connection)["provider_not_before"])
                connection.execute("UPDATE metadata SET value=? WHERE key='provider_not_before'",
                                   (str(max(prior, not_before)),))

    def mark_tick(self):
        with self._transaction() as connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='last_tick'", (str(self.clock()),))

    def retry(self, event_key):
        if not isinstance(event_key, str) or not re.fullmatch(r"[0-9a-f]{64}", event_key):
            raise NotificationError("NOTIFICATION_EVENT_ID_INVALID")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM deliveries WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                raise NotificationError("NOTIFICATION_EVENT_NOT_FOUND")
            if row["status"] != "REJECTED" or row["code"] not in _RETRYABLE:
                raise NotificationError("NOTIFICATION_RETRY_UNSAFE")
            if row["attempts"] >= _MAX_ATTEMPTS:
                raise NotificationError("NOTIFICATION_RETRY_EXHAUSTED")
            if row["not_before"] > self.clock():
                raise NotificationError("NOTIFICATION_RETRY_NOT_DUE")
            connection.execute(
                "UPDATE deliveries SET status='PENDING',code='NOTIFICATION_RETRY_QUEUED' WHERE event_key=?",
                (event_key,),
            )

    def status(self, *, limit=20, stale_after=60):
        with self._connection() as connection:
            meta = self._metadata(connection)
            counts = dict(connection.execute(
                """SELECT CASE WHEN status='SENDING' AND claimed_at<? THEN 'UNKNOWN'
                   ELSE status END AS category,COUNT(*) FROM deliveries GROUP BY category""",
                (self.clock() - stale_after,),
            ))
            rows = connection.execute(
                """SELECT event_key,status,attempts,code,message_id,claimed_at,not_before
                   FROM deliveries ORDER BY recorded_at DESC,event_key LIMIT ?""", (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = {key: row[key] for key in ("event_key", "status", "attempts", "code", "message_id")}
            if row["status"] == "SENDING" and self.clock() - row["claimed_at"] > stale_after:
                item["status"] = "UNKNOWN"
                item["code"] = "NOTIFICATION_INTERRUPTED_UNCONFIRMED"
            item["retry_after_seconds"] = max(0, int(row["not_before"] - self.clock() + .999))
            items.append(item)
        return {
            "enabled": True, "provider": "ntfy", "counts": counts,
            "last_tick": float(meta["last_tick"]),
            "cooldown_seconds": max(0, int(float(meta["provider_not_before"]) - self.clock() + .999)),
            "deliveries": items,
        }
