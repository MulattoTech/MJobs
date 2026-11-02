"""Read only schema evidence; no application rows or credentials are retained."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import urllib.request


SCRIPT = r'''
import json, os
from sqlalchemy import create_engine, text
from cli.runtime_database import runtime_environment
from cli.migration_precondition import (
    required_signature, observed_signature, validate_signature, digest, current_heads,
)
environment = runtime_environment(dict(os.environ))
engine = create_engine(environment["MJOBS_DATABASE_URL"],
                       connect_args={"connect_timeout": 8})
try:
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        required = required_signature()
        observed = observed_signature(connection, required)
        validate_signature(required, observed)
        privileges = dict(connection.execute(text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user")).mappings().one())
        assert not any(privileges.values()), "Runtime role has elevated privileges"
        result = {
            "result": "PASS",
            "transaction_read_only": connection.execute(text(
                "SHOW transaction_read_only")).scalar_one(),
            "schema_policy": required["policy_version"],
            "required_tables": len(required["tables"]),
            "observed_required_tables": len(observed["tables"]),
            "required_foreign_keys": sum(len(t["fks"]) for t in required["tables"].values()),
            "observed_foreign_keys": sum(len(t["fks"]) for t in observed["tables"].values()),
            "required_signature_sha256": digest(required),
            "observed_signature_sha256": digest(observed),
            "migration_heads": current_heads(connection),
            "runtime_role_flags": privileges,
        }
        assert result["transaction_read_only"] == "on"
        print(json.dumps(result))
finally:
    engine.dispose()
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with urllib.request.urlopen("http://127.0.0.1:18801/__deployment", timeout=6) as response:
        deployment = json.load(response)
    assert deployment["commit"] == args.expected
    result = subprocess.run(
        ["docker", "exec", "-i", "mjobs-micro-api-1", "python", "-"],
        input=SCRIPT, text=True, capture_output=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError("Read-only production schema verification failed")
    assert len(result.stdout) < 16384
    receipt = json.loads(result.stdout)
    receipt.update(timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   deployment=deployment, application_rows_read=False,
                   production_writes=False, credentials_retained=False)
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
