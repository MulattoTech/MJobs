"""Read-only deployed ORM/privilege witness; emit no profile or credential values.

Run inside the verified API image through cli.runtime_database, passing the
expected deployed Git SHA. This script never provisions, migrates or writes data.
"""

import json
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import event, select, text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from cli.provision_database import _has_unexpected_authority
from packages.persistence import models as m
from packages.persistence.store import Store


def main():
    step = "DEPLOYED_IDENTITY"
    store = None
    observed = set()

    def observe(state):
        observed.update(mapper.class_.__name__ for mapper in state.all_mappers)

    def facts(profile):
        return {fact['id']: fact for fact in profile['facts']}

    try:
        expected = sys.argv[1]
        if len(expected) != 40 or os.environ.get("MJOBS_DEPLOY_SHA") != expected:
            raise ValueError("identity")
        step = "READ_ONLY_DATABASE"
        store = Store(os.environ["MJOBS_DATABASE_URL"])
        event.listen(Session, "do_orm_execute", observe)
        with store.engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
            readonly = connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()
            isolation = connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
            step = "RUNTIME_AUTHORITY"
            role = connection.execute(text(
                "SELECT oid, rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls, session_user AS login_role FROM pg_roles WHERE rolname=current_user"
            )).mappings().one()
            flags = {name: role[name] for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls",
            )}
            unexpected = _has_unexpected_authority(connection, role["rolname"])
            membership_or_ownership = connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=:oid) "
                "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=:oid) "
                "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=:oid) "
                "OR EXISTS (SELECT 1 FROM pg_database WHERE datdba=:oid)"
            ), {"oid": role["oid"]}).scalar_one()
            protected_privileges = connection.execute(text(
                "SELECT has_database_privilege(current_user, current_database(), 'TEMPORARY') "
                "OR has_table_privilege(current_user, 'public.alembic_version', 'INSERT') "
                "OR has_table_privilege(current_user, 'public.alembic_version', 'UPDATE') "
                "OR has_table_privilege(current_user, 'public.alembic_version', 'DELETE')"
            )).scalar_one()
            if (role["rolname"] != "mjobs_runtime" or role["login_role"] != "mjobs_runtime"
                    or any(flags.values()) or unexpected
                    or membership_or_ownership or protected_privileges):
                raise ValueError("authority")
            step = "ORM_PROFILE_READ"
            profile_id = connection.execute(select(m.profiles.c.id).limit(1)).scalar_one_or_none()
            if profile_id is None:
                try:
                    store.profile("__mjobs_readonly_probe_empty__", connection=connection)
                except NoResultFound:
                    pass
                else:
                    raise ValueError("unexpected missing-profile result")
                if observed != {"CareerProfileRecord"}:
                    raise ValueError("missing-profile mapper")
                profile_result = "NO_PROFILE_ROWS"
            else:
                first = store.profile(profile_id, connection=connection)
                historical = store.profile(profile_id, first["version"], connection=connection)
                if facts(first) != facts(historical) or first["answers"] != historical["answers"]:
                    raise ValueError("historical read")
                if observed != {"CareerProfileRecord", "ProfileVersionRecord", "CareerFactRecord"}:
                    raise ValueError("mappers")
                profile_result = "PASS_CURRENT_AND_VERSIONED_READ"
            preserved = connection.get_transaction() is transaction
            if readonly != "on" or isolation != "repeatable read" or not preserved:
                raise ValueError("transaction")
            transaction.rollback()
        if profile_id is not None:
            step = "ORM_REOPEN"
            with store.engine.connect() as reopened:
                reopened.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                reopened.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                reopened.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
                again = store.profile(profile_id, first["version"], connection=reopened)
                if facts(first) != facts(again) or first["answers"] != again["answers"]:
                    raise ValueError("reopened version")
            profile_result = "PASS_CURRENT_VERSIONED_AND_REOPENED_READ"
        print(json.dumps({
            "result": "PASS", "recorded_at": datetime.now(UTC).isoformat(),
            "deployed_sha": expected, "transaction_read_only": readonly,
            "transaction_isolation": isolation, "authenticated_runtime_login": True,
            "runtime_role_flags": flags, "unexpected_effective_authority": unexpected,
            "role_membership_or_ownership": membership_or_ownership,
            "temporary_or_migration_write_authority": protected_privileges,
            "profile_read": profile_result, "observed_orm_classes": sorted(observed),
            "caller_transaction_preserved": preserved,
            "application_rows_read": profile_id is not None,
            "application_values_retained": False, "production_writes": False,
            "credentials_retained": False,
        }))
        return 0
    except Exception:  # noqa: BLE001 -- never print driver errors or credential values
        print(json.dumps({"result": "FAIL", "stage": step,
                          "production_writes": False, "private_details_retained": False}))
        return 1
    finally:
        if event.contains(Session, "do_orm_execute", observe):
            event.remove(Session, "do_orm_execute", observe)
        if store is not None:
            store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
