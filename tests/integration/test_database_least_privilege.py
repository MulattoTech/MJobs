"""Actual PostgreSQL acceptance: migration/admin isolation and populated runtime recovery."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from cli.provision_database import provision, runtime_url, ProvisionError
from packages.persistence.store import Store
from services.credentials.vault import CredentialCipher
from tests.integration.test_spine import PROFILE


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL is required")
class DatabaseLeastPrivilegePostgresTests(unittest.TestCase):
    def test_populated_upgrade_repeat_provision_privilege_denial_and_recovery(self):
        suffix = uuid4().hex
        database = "mjobs_privilege_" + suffix
        role = "mjobs_runtime_" + suffix
        original = make_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        cluster = create_engine(original, isolation_level="AUTOCOMMIT")
        admin_url = original.set(database=database).render_as_string(hide_password=False)
        admin = create_engine(admin_url)
        runtime = None
        store = None
        try:
            with cluster.connect() as connection:
                connection.connection.driver_connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            with patch.dict(os.environ, {"MJOBS_DATABASE_URL": admin_url}):
                command.upgrade(Config("alembic.ini"), "head")
            cipher = CredentialCipher.from_key_material("c3ludGhldGljLWNyZWRlbnRpYWwta2V5LW9ubHktMTI=")
            ciphertext = cipher.encrypt("fixture", "openai", "synthetic-secret-only")
            document = b"synthetic original resume bytes"
            document_hash = hashlib.sha256(document).hexdigest()
            store = Store(admin_url)
            store.save_profile(PROFILE)
            with admin.begin() as connection:
                connection.execute(text("CREATE TABLE preservation_probe (id serial PRIMARY KEY, ciphertext bytea, document_hash text)"))
                connection.execute(text("INSERT INTO preservation_probe (ciphertext, document_hash) VALUES (:ciphertext, :hash)"),
                                   {"ciphertext": ciphertext, "hash": document_hash})
                before = connection.execute(text("SELECT * FROM preservation_probe")).mappings().all()
            with tempfile.TemporaryDirectory() as root:
                destination = Path(root) / "runtime-url"
                provision(admin_url, destination, role=role)
                first = destination.read_bytes()
                provision(admin_url, destination, role=role)
                self.assertEqual(first, destination.read_bytes())
                self.assertEqual(destination.read_text(), runtime_url(admin_url, role))
                runtime = Store(destination.read_text())
                runtime.save_profile(dict(PROFILE, name="Reviewed synthetic profile"))
                self.assertEqual(runtime.profile("fixture")["name"], "Reviewed synthetic profile")
                with runtime.engine.begin() as connection:
                    self.assertTrue(MigrationContext.configure(connection).get_current_heads(),
                                    "Runtime readiness must read the migration version without being able to update it")
                    authority = connection.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
                    self.assertEqual(tuple(authority), (False,) * 5)
                    self.assertEqual(connection.execute(text("SELECT * FROM preservation_probe")).mappings().all(), before)
                    recovered = connection.execute(text("SELECT ciphertext FROM preservation_probe")).scalar_one()
                    self.assertEqual(cipher.decrypt("fixture", "openai", recovered), "synthetic-secret-only")
                    connection.execute(text("INSERT INTO preservation_probe (ciphertext, document_hash) VALUES (:ciphertext, :hash)"),
                                       {"ciphertext": ciphertext, "hash": document_hash})
                    connection.execute(text("UPDATE preservation_probe SET document_hash=:hash WHERE id=2"), {"hash": document_hash})
                    connection.execute(text("DELETE FROM preservation_probe WHERE id=2"))
                    connection.execute(text("SELECT pg_advisory_xact_lock(175)"))
                for forbidden in ("CREATE TABLE forbidden_ddl (id integer)", "CREATE TEMP TABLE forbidden_temp (id integer)",
                                  "CREATE SCHEMA forbidden_schema",
                                  "CREATE ROLE mjobs_forbidden_" + suffix,
                                  "CREATE DATABASE mjobs_forbidden_" + suffix, "UPDATE alembic_version SET version_num='invalid'",
                                  "TRUNCATE preservation_probe"):
                    with self.subTest(forbidden=forbidden):
                        with self.assertRaises(DBAPIError) as denied, runtime.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                            connection.execute(text(forbidden))
                        self.assertEqual(denied.exception.orig.sqlstate, "42501", "Must fail for insufficient privilege, not transaction syntax or missing objects")
                # Future administrator migrations still work and grant DML to new application tables.
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": admin_url}):
                    command.upgrade(Config("alembic.ini"), "head")
                with admin.begin() as connection:
                    connection.execute(text("CREATE TABLE future_probe (id serial PRIMARY KEY, value text)"))
                with runtime.engine.begin() as connection:
                    connection.execute(text("INSERT INTO future_probe (value) VALUES ('synthetic')"))
                # Lost generated-file recovery uses the original admin secret without rotating it.
                destination.unlink()
                provision(admin_url, destination, role=role)
                self.assertEqual(first, destination.read_bytes())
                # Role flags alone miss effective object ACLs, including PUBLIC.
                # Grant on an isolated synthetic database, prove the authority,
                # then ensure reuse fails before credentials or ACLs are changed.
                with admin.begin() as connection:
                    connection.execute(text("CREATE SCHEMA acl_probe"))
                    connection.execute(text(
                        "CREATE FUNCTION public.acl_probe_trigger() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN RETURN NEW; END $$"))
                targets = [
                    ("DATABASE", database, "CREATE", "has_database_privilege"),
                    ("SCHEMA", "acl_probe", "CREATE", "has_schema_privilege"),
                    ("SEQUENCE", "preservation_probe_id_seq", "UPDATE", "has_sequence_privilege"),
                    ("TABLE", "preservation_probe", "TRUNCATE", "has_table_privilege"),
                    ("TABLE", "preservation_probe", "TRIGGER", "has_table_privilege"),
                ]
                for kind, target, privilege, function in targets:
                    for grantee in (sql.Identifier(role), sql.SQL("PUBLIC")):
                        with self.subTest(kind=kind, privilege=privilege, grantee=str(grantee)):
                            grant = sql.SQL("GRANT {} ON {} {} TO {}").format(
                                sql.SQL(privilege), sql.SQL(kind), sql.Identifier(target), grantee)
                            revoke = sql.SQL("REVOKE {} ON {} {} FROM {}").format(
                                sql.SQL(privilege), sql.SQL(kind), sql.Identifier(target), grantee)
                            try:
                                with admin.begin() as connection:
                                    connection.connection.driver_connection.execute(grant)
                                with runtime.engine.connect() as connection:
                                    # Function names come only from the fixed list above;
                                    # all database/role/object values remain bound.
                                    self.assertTrue(connection.execute(text(
                                        f"SELECT {function}(current_user, :target, :privilege)"),
                                        {"target": target, "privilege": privilege}).scalar_one())
                                    if kind == "DATABASE":
                                        connection.execute(text("CREATE SCHEMA rollback_acl_proof"))
                                    elif kind == "SCHEMA":
                                        connection.execute(text("CREATE TABLE acl_probe.rollback_acl_proof (id integer)"))
                                    elif kind == "SEQUENCE":
                                        # Sequences are not transactional; retain their exact current
                                        # value/is_called while proving setval authorization.
                                        connection.execute(text(
                                            "SELECT setval('preservation_probe_id_seq', last_value, is_called) "
                                            "FROM preservation_probe_id_seq"))
                                    elif privilege == "TRUNCATE":
                                        connection.execute(text("TRUNCATE preservation_probe"))
                                    elif privilege == "TRIGGER":
                                        connection.execute(text(
                                            "CREATE TRIGGER rollback_acl_proof BEFORE INSERT ON preservation_probe "
                                            "FOR EACH ROW EXECUTE FUNCTION public.acl_probe_trigger()"))
                                    # Closing without commit rolls back synthetic DDL/truncation.
                                with self.assertRaisesRegex(ProvisionError, "unexpected authority"):
                                    provision(admin_url, destination, role=role)
                                self.assertEqual(first, destination.read_bytes())
                                if grantee == sql.SQL("PUBLIC"):
                                    fresh_role = "mjobs_fresh_" + suffix
                                    with self.assertRaisesRegex(ProvisionError, "unexpected authority"):
                                        provision(admin_url, destination, role=fresh_role)
                                    self.assertEqual(first, destination.read_bytes())
                                    with admin.connect() as connection:
                                        self.assertFalse(connection.execute(text(
                                            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                                            {"role": fresh_role}).scalar_one(), "Fresh unsafe role creation must roll back")
                            finally:
                                with admin.begin() as connection:
                                    connection.connection.driver_connection.execute(revoke)
                            provision(admin_url, destination, role=role)
                            self.assertEqual(first, destination.read_bytes())
                with admin.begin() as connection:
                    connection.connection.driver_connection.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(role)))
                with self.assertRaises(ProvisionError):
                    provision(admin_url, destination, role=role)
                self.assertEqual(first, destination.read_bytes())
                with admin.begin() as connection:
                    driver = connection.connection.driver_connection
                    driver.execute(sql.SQL("ALTER ROLE {} NOCREATEDB PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal("wrong-synthetic-runtime-password")))
                with self.assertRaises(ProvisionError):
                    provision(admin_url, destination, role=role)
                self.assertEqual(first, destination.read_bytes())
                with admin.connect() as connection:
                    self.assertEqual(connection.execute(text("SELECT * FROM preservation_probe")).mappings().all(), before)
        finally:
            if runtime:
                runtime.engine.dispose()
            if store:
                store.engine.dispose()
            admin.dispose()
            with cluster.connect() as connection:
                driver = connection.connection.driver_connection
                driver.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
                driver.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
                driver.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier("mjobs_forbidden_" + suffix)))
                driver.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier("mjobs_forbidden_" + suffix)))
            cluster.dispose()
