"""Migrate with administrator credentials, then provision the restricted application login.

The original administrator password and encryption key are never changed. A versioned
HMAC derives an independent runtime password so a database restore can reconstruct it
from the same protected configuration without introducing an unbacked recovery secret.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import sys
import tempfile

from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cli.migration_precondition import migrate_guarded, strict_json


RUNTIME_ROLE = "mjobs_runtime"


class ProvisionError(ValueError):
    """Sanitized provisioning failure."""


def runtime_url(admin_url: str, role: str = RUNTIME_ROLE) -> str:
    url = make_url(admin_url)
    if url.drivername != "postgresql+psycopg" or not url.password or not url.database or url.username == role:
        raise ProvisionError("An administrator PostgreSQL URL is required")
    context = ("MJobs database runtime credential v1\0" + url.database + "\0" + role).encode("utf-8")
    password = hmac.new(url.password.encode("utf-8"), context, hashlib.sha256).hexdigest()
    return url.set(username=role, password=password).render_as_string(hide_password=False)


def _verify_login(url: str) -> None:
    engine = create_engine(url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise ProvisionError("Existing runtime login does not match protected configuration; no password was rotated") from None
    finally:
        engine.dispose()


def _has_unexpected_authority(connection, role: str) -> bool:
    """Check effective ACLs, including PUBLIC grants and non-public namespaces."""
    return connection.execute(text(
        "SELECT has_database_privilege(:role, current_database(), 'CREATE') "
        "OR EXISTS (SELECT 1 FROM pg_namespace "
        "WHERE has_schema_privilege(:role, oid, 'CREATE')) "
        # Boolean WHERE terms can be reordered. CASE is required here because
        # sequence privilege inquiry raises for non-sequence catalog objects.
        "OR EXISTS (SELECT 1 FROM pg_class WHERE CASE "
        "WHEN relkind IN ('r', 'p', 'v', 'm', 'f') THEN "
        "(has_table_privilege(:role, oid, 'TRUNCATE') "
        "OR has_table_privilege(:role, oid, 'TRIGGER')) ELSE FALSE END) "
        "OR EXISTS (SELECT 1 FROM pg_class WHERE CASE WHEN relkind='S' THEN "
        "has_sequence_privilege(:role, oid, 'UPDATE') ELSE FALSE END)"), {"role": role}).scalar_one()


def provision(admin_url: str, destination: Path, *, role: str = RUNTIME_ROLE) -> None:
    """Idempotently grant application DML, excluding migration bookkeeping and all DDL."""
    login_url = runtime_url(admin_url, role)
    admin = create_engine(admin_url, connect_args={"connect_timeout": 10})
    try:
        with admin.begin() as connection:
            # Serialize concurrent startup of this dedicated database's provisioning service.
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                               {"key": "mjobs-runtime-provision:" + role})
            existing = connection.execute(text(
                "SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname=:role"), {"role": role}).mappings().one_or_none()
            if existing:
                if (any(existing[name] for name in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"))
                        or not existing["rolcanlogin"]
                        or connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=:oid) "
                                                   "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=:oid) "
                                                   "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=:oid) "
                                                   "OR EXISTS (SELECT 1 FROM pg_database WHERE datdba=:oid)"),
                                              {"oid": existing["oid"]}).scalar_one()
                        or _has_unexpected_authority(connection, role)):
                    raise ProvisionError("Existing runtime role has unexpected authority; refusing to reuse it")
                _verify_login(login_url)
            driver = connection.connection.driver_connection
            if not existing:
                driver.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                       "NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
                                           sql.Identifier(role), sql.Literal(make_url(login_url).password)))
            database = connection.execute(text("SELECT current_database()")).scalar_one()
            for statement in (
                sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)),
                sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(sql.Identifier(database), sql.Identifier(role)),
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)),
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
                sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(role)),
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)),
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(sql.Identifier(role)),
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(role)),
                sql.SQL("REVOKE ALL ON TABLE public.alembic_version FROM {}").format(sql.Identifier(role)),
                sql.SQL("GRANT SELECT ON TABLE public.alembic_version TO {}").format(sql.Identifier(role)),
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(role)),
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(role)),
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(role)),
            ):
                driver.execute(statement)
            # Fresh roles can inherit unsafe PUBLIC ACLs too. Refuse to publish a
            # credential rather than silently alter unrelated schemas/privileges.
            if _has_unexpected_authority(connection, role):
                raise ProvisionError("Runtime role has unexpected authority; refusing to publish it")
        _verify_login(login_url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ProvisionError("Runtime credential destination is unsafe")
        # Never truncate the last usable credential on a failed migration/provision attempt.
        descriptor, temporary = tempfile.mkstemp(prefix=".runtime-url-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                os.chmod(temporary, 0o600)
                stream.write(login_url)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        admin.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision-only", action="store_true", help="Provision a restored database without migrating it")
    parser.add_argument("--migration-run", help="One-use authorization created by cli.guarded_migration")
    arguments = parser.parse_args()
    try:
        admin_url = os.environ["MJOBS_DATABASE_URL"]
        runtime_url(admin_url)  # validate before any database/migration side effect
        if arguments.provision_only and arguments.migration_run is not None:
            raise ProvisionError("Ambiguous provisioning action")
        if not arguments.provision_only:
            nonce = None
            if arguments.migration_run is not None:
                raw = sys.stdin.buffer.read(4097)
                if len(raw) > 4096:
                    raise ProvisionError("Migration authorization input exceeds its bound")
                request = strict_json(raw)
                if not isinstance(request, dict) or set(request) != {"nonce"}:
                    raise ProvisionError("Invalid migration authorization input")
                nonce = request["nonce"]
            migrate_guarded(admin_url, Config("alembic.ini"), run_id=arguments.migration_run, nonce=nonce)
        provision(admin_url, Path(os.environ["MJOBS_DATABASE_URL_FILE"]))
    except Exception:
        # Driver exceptions may contain connection URLs or SQL literals. Keep them private.
        print("Database migration/provision failed; preserve existing data, credentials and backup for recovery", file=sys.stderr)
        return 1
    print("Database migration and restricted runtime login ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
