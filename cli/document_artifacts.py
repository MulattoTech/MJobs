"""Inspect and explicitly recover local generated-document artifacts."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from packages.persistence.store import Store
from services.documents.artifacts import (
    DEFAULT_QUARANTINE_RETENTION,
    list_quarantined,
    purge_expired_quarantine,
    quarantine_artifact,
    restore_quarantined,
    scan_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "list", "quarantine", "restore", "purge"):
        command = commands.add_parser(name)
        command.add_argument("--output-dir", type=Path, required=True,
                             help="explicit local document storage root")
        if name == "quarantine":
            command.add_argument("--artifact-id", required=True)
        elif name == "restore":
            command.add_argument("--quarantine-id", required=True)
        elif name == "purge":
            command.add_argument("--retention-days", type=float,
                                 default=DEFAULT_QUARANTINE_RETENTION.total_seconds() / 86400)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    database_url = os.environ.get("MJOBS_DATABASE_URL")
    if not database_url:
        parser.error("MJOBS_DATABASE_URL must be supplied through protected environment configuration")
    if args.command == "purge" and args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    store = Store(database_url)
    try:
        status = store.workspace_status()
        if status["state"] != "ACTIVE":
            parser.error("The selected database is not bound to a workspace mode")
        if args.command == "scan":
            result = scan_artifacts(store, args.output_dir)
        elif args.command == "list":
            result = list_quarantined(args.output_dir)
        elif args.command == "quarantine":
            result = quarantine_artifact(store, args.output_dir, args.artifact_id)
        elif args.command == "restore":
            result = restore_quarantined(store, args.output_dir, args.quarantine_id)
        else:
            result = purge_expired_quarantine(
                store, args.output_dir, retention=timedelta(days=args.retention_days))
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        parser.error(str(error))
    finally:
        store.engine.dispose()
    return 2


if __name__ == "__main__":
    sys.exit(main())
