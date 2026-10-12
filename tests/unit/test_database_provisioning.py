"""Runtime credentials are distinct, deterministic for recovery, and URL-safe."""
import unittest
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from cli.provision_database import provision, runtime_url, ProvisionError


class DatabaseProvisioningTests(unittest.TestCase):
    def test_fresh_role_inheriting_public_authority_cannot_publish_credentials(self):
        connection = MagicMock()

        def execute(statement, parameters=None):
            result = MagicMock()
            query = str(statement)
            if "FROM pg_roles" in query:
                result.mappings.return_value.one_or_none.return_value = None
            elif "has_database_privilege" in query:
                result.scalar_one.return_value = True
            elif "current_database()" in query:
                result.scalar_one.return_value = "mjobs_test"
            return result

        connection.execute.side_effect = execute
        admin = MagicMock()
        admin.begin.return_value.__enter__.return_value = connection
        with tempfile.TemporaryDirectory() as root, patch(
                "cli.provision_database.create_engine", return_value=admin), patch(
                "cli.provision_database._verify_login") as login:
            destination = Path(root) / "runtime-url"
            destination.write_bytes(b"original protected credential")
            with self.assertRaisesRegex(ProvisionError, "unexpected authority"):
                provision("postgresql+psycopg://admin:synthetic@localhost/mjobs_test", destination)
            self.assertEqual(destination.read_bytes(), b"original protected credential")
            login.assert_not_called()

    def test_effective_unexpected_authority_refuses_reuse_without_replacing_credentials(self):
        connection = MagicMock()
        role = dict(oid=123, rolsuper=False, rolcreatedb=False, rolcreaterole=False,
                    rolreplication=False, rolbypassrls=False, rolcanlogin=True)

        def execute(statement, parameters=None):
            result = MagicMock()
            query = str(statement)
            if "FROM pg_roles" in query:
                result.mappings.return_value.one_or_none.return_value = role
            elif "pg_auth_members" in query:
                result.scalar_one.return_value = False
            elif "has_database_privilege" in query:
                result.scalar_one.return_value = True
            elif "current_database()" in query:
                result.scalar_one.return_value = "mjobs_test"
            else:
                result.scalar_one.return_value = False
            return result

        connection.execute.side_effect = execute
        admin = MagicMock()
        admin.begin.return_value.__enter__.return_value = connection
        with tempfile.TemporaryDirectory() as root, patch(
                "cli.provision_database.create_engine", return_value=admin), patch(
                "cli.provision_database._verify_login") as login:
            destination = Path(root) / "runtime-url"
            destination.write_bytes(b"original protected credential")
            with self.assertRaisesRegex(ProvisionError, "unexpected authority"):
                provision("postgresql+psycopg://admin:synthetic@localhost/mjobs_test", destination)
            self.assertEqual(destination.read_bytes(), b"original protected credential")
            login.assert_not_called()
            connection.connection.driver_connection.execute.assert_not_called()

    def test_runtime_credential_is_distinct_stable_and_bound_to_database_and_role(self):
        original = make_url("postgresql+psycopg://mjobs:synthetic@postgres/mjobs").set(password="synthetic/@:'%only")
        source = original.render_as_string(hide_password=False)
        first = runtime_url(source)
        parsed = make_url(first)
        self.assertEqual(first, runtime_url(source))
        self.assertEqual(parsed.username, "mjobs_runtime")
        self.assertEqual(parsed.host, original.host)
        self.assertEqual(parsed.database, original.database)
        self.assertNotEqual(parsed.password, original.password)
        self.assertNotIn(original.password, first)
        self.assertNotEqual(parsed.password, make_url(runtime_url(source, "other_runtime")).password)
        other_database = original.set(database="other").render_as_string(hide_password=False)
        self.assertNotEqual(parsed.password, make_url(runtime_url(other_database)).password)
        self.assertEqual(original.password, make_url(source).password)

    def test_no_admin_credential_is_generated_for_missing_or_runtime_input(self):
        for url in ("sqlite://", "postgresql+psycopg://mjobs@postgres/mjobs",
                    "postgresql+psycopg://mjobs_runtime:secret@postgres/mjobs"):
            with self.subTest(url=url), self.assertRaises(ProvisionError):
                runtime_url(url)
