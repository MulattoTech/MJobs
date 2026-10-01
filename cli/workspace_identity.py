"""Inspect or deliberately bind a legacy database's operation mode."""
import argparse
import json
import os

from sqlalchemy.exc import SQLAlchemyError

from packages.domain.operation_mode import WorkspaceModeError
from packages.persistence.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect", help="Read sanitized row counts and a whole-database fingerprint")
    adopt = actions.add_parser("adopt", help="Bind reviewed legacy data to one operation mode")
    adopt.add_argument("--mode", required=True, choices=("FIXTURE", "PERSONAL"))
    adopt.add_argument("--expect-fingerprint", required=True)
    adopt.add_argument("--acknowledge-reviewed-data", action="store_true", required=True)
    args = parser.parse_args(argv)
    if "MJOBS_DATABASE_URL" not in os.environ:
        parser.error("MJOBS_DATABASE_URL must be supplied through protected environment configuration")
    store = None
    try:
        store = Store(os.environ["MJOBS_DATABASE_URL"])
        if args.action == "inspect":
            result = store.workspace_status()
        else:
            result = store.adopt_legacy_workspace_mode(args.mode, args.expect_fingerprint)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except WorkspaceModeError as error:
        print(json.dumps({"result": "FAIL", "code": error.code}))
        return 1
    except SQLAlchemyError as error:
        print(json.dumps({"result": "FAIL", "code": "DATABASE_UNAVAILABLE", "error_type": type(error).__name__}))
        return 1
    except ValueError:
        print(json.dumps({"result": "FAIL", "code": "INVALID_WORKSPACE_REQUEST"}))
        return 1
    finally:
        if store is not None:
            store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
