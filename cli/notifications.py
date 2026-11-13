"""Opt-in ntfy delivery, sanitized status and explicitly safe retries.

Use: python -m cli.notifications --config <private-file> run [--once]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from services.notifications.configuration import load_config
from services.notifications.contract import NotificationError
from services.notifications.journal import DeliveryJournal
from services.notifications.providers import capability_descriptors, create_provider
from services.notifications.runner import run_once


def _print(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Private configuration file; otherwise MJOBS_NOTIFICATION_CONFIG")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Deliver new profile-owned terminal activity")
    run.add_argument("--once", action="store_true", help="One bounded scan and at most one send")
    status = commands.add_parser("status", help="Sanitized delivery status, with no network calls")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1..200")
    retry = commands.add_parser("retry", help="Queue an explicitly rejected event for a later run")
    retry.add_argument("event_key")
    commands.add_parser("health", help="Check notification-worker heartbeat without network calls")
    commands.add_parser("capabilities", help="Report implemented provider surfaces and limitations")
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        _print({"providers": capability_descriptors()})
        return 0
    try:
        config = load_config(args.config)
        if not config.enabled:
            _print({"enabled": False, "result": "DISABLED"})
            return 0
        if args.command != "run" and not Path(config.state_path).is_file():
            if args.command == "retry":
                raise NotificationError("NOTIFICATION_EVENT_NOT_FOUND")
            _print({"enabled": True, "result": "NOT_INITIALIZED"})
            return 1 if args.command == "health" else 0
        journal = DeliveryJournal(config)
        if args.command == "status":
            _print(journal.status(limit=args.limit, stale_after=config.request_timeout + 5))
            return 0
        if args.command == "retry":
            journal.retry(args.event_key)
            _print({"result": "RETRY_QUEUED", "event_key": args.event_key})
            return 0
        if args.command == "health":
            status_value = journal.status(limit=1, stale_after=config.request_timeout + 5)
            healthy = time.time() - status_value["last_tick"] <= config.poll_seconds * 3 + config.request_timeout
            _print({"healthy": healthy})
            return 0 if healthy else 1

        from packages.persistence.store import Store
        database_url = os.environ.get("MJOBS_DATABASE_URL")
        if not database_url:
            raise NotificationError("NOTIFICATION_DATABASE_UNAVAILABLE")
        store = Store(database_url)
        stopped = threading.Event()
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_args: stopped.set())
        try:
            provider = create_provider(config)
            while not stopped.is_set():
                _print(run_once(store, config, journal, provider))
                if args.once:
                    break
                stopped.wait(config.poll_seconds)
        finally:
            store.engine.dispose()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    except NotificationError as error:
        _print({"result": "ERROR", "code": error.code})
        return 2
    except Exception:
        # Provider payloads, secrets, connection URLs and private paths never become diagnostics.
        _print({"result": "ERROR", "code": "NOTIFICATION_RUNTIME_UNAVAILABLE"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
