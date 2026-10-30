"""Sustained SQLite enforcement checks for the core relational spine."""

import ast
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, event, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


TIMESTAMP = "2026-10-24T12:00:00Z"
PARENT_REVISION = "20261018cm01"
TARGET_REVISION = "20261024dc01"
APPLICATION_STATES = m.APPLICATION_STATES
APPLICATION_DIRECT_WRITER_PATHS = (
    "services/applications/submission_admission.py",
    "services/documents/generate.py",
    "services/applications/external_capture.py",
    "services/applications/confirmation_recovery.py",
)
APPLICATION_WRITER_PATHS = (
    "services/applications/mock_adapter.py",
    *APPLICATION_DIRECT_WRITER_PATHS,
)
APPLICATION_WRITE_BOUNDARY_PATHS = (
    *APPLICATION_DIRECT_WRITER_PATHS,
    "packages/persistence/profile_restore.py",
    "packages/persistence/store.py",
)
REBUILT_TABLES = ("job_matches", "applications")


def _uppercase_literals(node):
    return {
        item.value
        for item in ast.walk(node)
        if (isinstance(item, ast.Constant) and isinstance(item.value, str)
            and item.value and item.value == item.value.upper()
            and item.value.replace("_", "").isalpha())
    }


def _application_writer_states(root):
    states = set()
    for relative_path in APPLICATION_WRITER_PATHS:
        path = root / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "transition" and len(node.args) >= 3):
                states.update(_uppercase_literals(node.args[2]))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "values"):
                owner = ast.get_source_segment(source, node.func.value) or ""
                if "m.applications" in owner:
                    for keyword in node.keywords:
                        if keyword.arg == "state":
                            states.update(_uppercase_literals(keyword.value))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "upsert" and len(node.args) >= 3
                    and ast.get_source_segment(source, node.args[1]) == "m.applications"
                    and isinstance(node.args[2], ast.Dict)):
                for key, value in zip(node.args[2].keys, node.args[2].values):
                    if isinstance(key, ast.Constant) and key.value == "state":
                        states.update(_uppercase_literals(value))
            if (relative_path.endswith("confirmation_recovery.py")
                    and isinstance(node, (ast.Assign, ast.AnnAssign))):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "state" for target in targets):
                    states.update(_uppercase_literals(node.value))
    return states


def _profile_restore_state_set(root, variable):
    path = root / "packages/persistence/profile_restore.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    states = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == variable
                        for target in node.targets)):
            states.update(_uppercase_literals(node.value))
    return states


def _profile_restore_normalized_states(root):
    path = root / "packages/persistence/profile_restore.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    states = set()
    application_loops = [
        node for node in ast.walk(tree)
        if (isinstance(node, ast.For)
            and ast.get_source_segment(source, node.iter) == 'history["applications"]')
    ]
    if len(application_loops) != 1:
        raise AssertionError("Expected one bounded application restore loop")
    for node in ast.walk(application_loops[0]):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
                (isinstance(target, ast.Name) and target.id == "state")
                or (isinstance(target, (ast.Tuple, ast.List))
                    and any(isinstance(item, ast.Name) and item.id == "state"
                            for item in target.elts))
                for target in targets):
            states.update(_uppercase_literals(node.value))
    return states


def _audit_history_application_states(root):
    path = root / "services/audit_history.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_APPLICATION_EVENTS"
                        for target in node.targets)
                and isinstance(node.value, ast.Dict)):
            return {
                key.value
                for key in node.value.keys
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and "_" not in key.value)
            }
    raise AssertionError("Expected bounded audit-history application event mapping")


def _application_write_boundary_paths(root):
    paths = set()
    for base in (root / "services", root / "packages"):
        for path in base.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "m.applications" not in source:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                rendered = ast.get_source_segment(source, node) or ""
                is_upsert = (isinstance(node.func, ast.Name) and node.func.id == "upsert"
                             and len(node.args) >= 2
                             and ast.get_source_segment(source, node.args[1]) == "m.applications")
                is_direct_write = ("m.applications" in rendered
                                   and (".insert()" in rendered or "update(m.applications)" in rendered))
                if is_upsert or is_direct_write:
                    paths.add(path.relative_to(root).as_posix())
                    break
    return paths


class DatabaseConstraintsSustainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-database-constraints-")
        self.addCleanup(self.temp.cleanup)
        self.database_url = f"sqlite:///{(Path(self.temp.name) / 'constraints.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company",
                name="Fixture Company",
                domain="fixture.example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id="job",
                company_id="company",
                requisition="REQ-1",
                title="Fixture Engineer",
                description="Controlled fixture description",
                skills=[],
                location="Remote",
                remote_type="REMOTE",
                attributes={},
            ))
            connection.execute(m.routes.insert().values(
                id="route",
                job_id="job",
                company_id="company",
                provider="mock-ats",
                tenant="fixture",
                requisition="REQ-1",
                url="https://fixture.example.test/apply",
            ))

    def test_valid_rows_reopen_with_sqlite_foreign_keys_enabled(self):
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="match",
                job_id="job",
                profile_id="fixture",
                profile_version="v1",
                score=95,
                missing=[],
                components={},
            ))
            connection.execute(m.applications.insert().values(
                id="application",
                job_id="job",
                route_id="route",
                profile_id="fixture",
                profile_version="v1",
                state="DRAFT",
                target="MOCK",
                fact_ids=[],
                trace={},
                document_version=1,
                confirmation=None,
                created_at=TIMESTAMP,
                submitted_at=None,
            ))

        reopened = Store(self.database_url)
        try:
            with reopened.engine.connect() as connection:
                self.assertEqual(connection.execute(select(m.matches.c.id)).scalar_one(), "match")
                self.assertEqual(
                    connection.execute(select(m.applications.c.id)).scalar_one(),
                    "application",
                )
        finally:
            reopened.engine.dispose()

    def test_match_score_outside_zero_to_one_hundred_rejects_atomically(self):
        # sqlite3 binds NaN as NULL, so that case proves only local fail-closed
        # behavior through NOT NULL; it is not evidence for PostgreSQL CHECK semantics.
        for index, score in enumerate((-0.01, 100.01, float("inf"), float("-inf"), float("nan"))):
            with self.subTest(score=score), self.assertRaises(IntegrityError):
                with self.store.engine.begin() as connection:
                    connection.execute(m.matches.insert().values(
                        id=f"impossible-score-{index}",
                        job_id="job",
                        profile_id="fixture",
                        profile_version="v1",
                        score=score,
                        missing=[],
                        components={},
                    ))
        self.assertEqual(self.store.count("job_matches"), 0)

        with self.store.engine.begin() as connection:
            for score in (0, 100):
                connection.execute(m.matches.insert().values(
                    id=f"boundary-score-{score}",
                    job_id="job",
                    profile_id="fixture",
                    profile_version="v1",
                    score=score,
                    missing=[],
                    components={},
                ))
        self.assertEqual(self.store.count("job_matches"), 2)

    def test_unknown_application_state_rejects_atomically(self):
        with self.assertRaises(IntegrityError):
            with self.store.engine.begin() as connection:
                connection.execute(m.applications.insert().values(
                    id="impossible-state",
                    job_id="job",
                    route_id="route",
                    profile_id="fixture",
                    profile_version="v1",
                    state="NOT_A_REAL_APPLICATION_STATE",
                    target="MOCK",
                    fact_ids=[],
                    trace={},
                    document_version=1,
                    confirmation=None,
                    created_at=TIMESTAMP,
                    submitted_at=None,
                ))
        self.assertEqual(self.store.count("applications"), 0)

    def test_every_writer_derived_application_state_remains_admissible(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.applications.insert().values(
                id="state-contract",
                job_id="job",
                route_id="route",
                profile_id="fixture",
                profile_version="v1",
                state="DRAFT",
                target="MOCK",
                fact_ids=[],
                trace={},
                document_version=1,
                confirmation=None,
                created_at=TIMESTAMP,
                submitted_at=None,
            ))
        for state in APPLICATION_STATES:
            with self.subTest(state=state), self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(
                    m.applications.c.id == "state-contract"
                ).values(state=state))
        self.assertEqual(self.store.application("state-contract")["state"], "UNKNOWN")


class DatabaseConstraintMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _migrate(self, config, url, revision, *, downgrade=False):
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)

    def _migration_module(self, config):
        return ScriptDirectory.from_config(config).get_revision(TARGET_REVISION).module

    def _legacy_database(self, directory, filename, *, score=95, state="PREPARED"):
        url = f"sqlite:///{(Path(directory) / filename).as_posix()}"
        config = self._config(url)
        self._migrate(config, url, PARENT_REVISION)
        engine = create_engine(url, poolclass=NullPool)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO career_profiles (id,name,email,version) VALUES "
                "('fixture','Fixture Person','fixture@example.test','v1')"
            ))
            connection.execute(text(
                "INSERT INTO companies (id,name,domain) VALUES "
                "('company','Fixture Company','fixture.example.test')"
            ))
            connection.execute(text(
                "INSERT INTO canonical_jobs "
                "(id,company_id,requisition,title,description,skills,location,remote_type,attributes) "
                "VALUES ('job','company','REQ-1','Fixture Engineer','Controlled fixture',"
                "'[]','Remote','REMOTE','{}')"
            ))
            connection.execute(text(
                "INSERT INTO application_routes "
                "(id,job_id,company_id,provider,tenant,requisition,url) VALUES "
                "('route','job','company','mock-ats','fixture','REQ-1',"
                "'https://fixture.example.test/apply')"
            ))
            connection.execute(text(
                "INSERT INTO job_matches "
                "(id,job_id,profile_id,profile_version,score,missing,components) "
                "VALUES ('match','job','fixture','v1',:score,'[]','{}')"
            ), {"score": score})
            connection.execute(text(
                "INSERT INTO applications "
                "(id,job_id,route_id,profile_id,profile_version,state,target,fact_ids,trace,"
                "document_version,confirmation,created_at,submitted_at) VALUES "
                "('application','job','route','fixture','v1',:state,'MOCK','[]','{}',"
                "1,NULL,:created,NULL)"
            ), {"state": state, "created": TIMESTAMP})
            connection.execute(text(
                "INSERT INTO application_route_choices "
                "(id,application_id,route_id,underlying_key,requested_mode,state,created_at) "
                "VALUES ('route-choice','application','route','fixture-route','THIS_ROUTE',"
                "'PLANNED',:created)"
            ), {"created": TIMESTAMP})
            connection.execute(text(
                "INSERT INTO documents "
                "(id,application_id,kind,version,profile_version,files,fact_ids,sha256,template,"
                "created_at,generation_model,generation_context) VALUES "
                "('document','application','resume',1,'v1','{}','[]','{}','fixture',"
                ":created,NULL,NULL)"
            ), {"created": TIMESTAMP})
            connection.execute(text(
                "INSERT INTO application_attempts (id,application_id,state,steps,started_at) "
                "VALUES ('attempt','application','RUNNING','[]',:started)"
            ), {"started": TIMESTAMP})
        return url, config, engine

    @staticmethod
    def _check_names(engine, table):
        return set(DatabaseConstraintMigrationTests._check_signature(engine, table))

    @staticmethod
    def _normalize_check_sql(value):
        normalized = "".join(str("" if value is None else value).upper().split())
        while normalized.startswith("(") and normalized.endswith(")"):
            normalized = normalized[1:-1]
        return normalized.replace('"', "").replace("`", "").replace("[", "").replace("]", "")

    @staticmethod
    def _check_signature(engine, table):
        checks = inspect(engine).get_check_constraints(table)
        if any(not item["name"] for item in checks):
            raise AssertionError(f"Unnamed CHECK constraint on {table}")
        return {
            item["name"]: DatabaseConstraintMigrationTests._normalize_check_sql(item["sqltext"])
            for item in checks
        }

    @staticmethod
    def _metadata_check_signature(table):
        return {
            constraint.name: DatabaseConstraintMigrationTests._normalize_check_sql(
                constraint.sqltext
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }

    def _expected_target_checks(self, parent_checks):
        additions = {
            "job_matches": self._metadata_check_signature(m.matches),
            "applications": self._metadata_check_signature(m.applications),
        }
        expected = {}
        for table in REBUILT_TABLES:
            overlap = set(parent_checks[table]) & set(additions[table])
            self.assertEqual(overlap, set(), f"target CHECK already exists at parent for {table}")
            expected[table] = {**parent_checks[table], **additions[table]}
        return expected

    def _check_snapshot(self, engine):
        return {table: self._check_signature(engine, table) for table in REBUILT_TABLES}

    @staticmethod
    def _schema_signature(engine, table):
        inspector = inspect(engine)
        return {
            "columns": tuple(
                (column["name"], column["nullable"], str(column.get("default")))
                for column in inspector.get_columns(table)
            ),
            "foreign_keys": tuple(sorted(
                (tuple(item["constrained_columns"]), item["referred_table"],
                 tuple(item["referred_columns"]))
                for item in inspector.get_foreign_keys(table)
            )),
            "indexes": tuple(sorted(
                (item["name"], tuple(item["column_names"]), bool(item["unique"]))
                for item in inspector.get_indexes(table)
            )),
            "uniques": tuple(sorted(
                (str(item["name"]), tuple(item["column_names"]))
                for item in inspector.get_unique_constraints(table)
            )),
        }

    @staticmethod
    def _row_snapshot(engine):
        tables = (
            "job_matches",
            "applications",
            "application_route_choices",
            "documents",
            "application_attempts",
        )
        with engine.connect() as connection:
            return {
                table: [dict(row) for row in connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")
                ).mappings()]
                for table in tables
            }

    def test_state_policy_matches_bounded_writers_and_legacy_normalization(self):
        root = Path(__file__).resolve().parents[2]
        config = self._config("sqlite://")
        migration = self._migration_module(config)
        writer_states = _application_writer_states(root)
        ambiguous_states = _profile_restore_state_set(root, "ambiguous_states")
        terminal_states = _profile_restore_state_set(root, "terminal_states")
        normalized_states = _profile_restore_normalized_states(root)
        audit_history_states = _audit_history_application_states(root)

        self.assertEqual(
            _application_write_boundary_paths(root),
            set(APPLICATION_WRITE_BOUNDARY_PATHS),
        )
        self.assertEqual(set(m.APPLICATION_STATES), set(migration.APPLICATION_STATES))
        self.assertEqual(writer_states, set(migration.WRITER_APPLICATION_STATES))
        self.assertEqual(terminal_states, set(migration.RESTORED_TERMINAL_APPLICATION_STATES))
        self.assertEqual(
            ambiguous_states - set(migration.APPLICATION_STATES),
            set(migration.DEPRECATED_AMBIGUOUS_APPLICATION_STATES),
        )
        self.assertEqual(
            normalized_states,
            set(migration.RESTORE_NORMALIZED_APPLICATION_STATES),
        )
        self.assertEqual(
            set(migration.APPLICATION_STATES),
            writer_states | terminal_states | set(migration.LEGACY_APPLICATION_STATES),
        )
        self.assertTrue(audit_history_states.issubset(migration.APPLICATION_STATES))
        self.assertIn("SUBMISSION_UNCONFIRMED", migration.APPLICATION_STATES)

    def test_upgrade_downgrade_and_reupgrade_preserve_valid_rows(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-constraint-migration-") as directory:
            url, config, engine = self._legacy_database(directory, "valid.db")
            try:
                tracked_tables = (
                    "job_matches",
                    "applications",
                    "application_route_choices",
                    "documents",
                    "application_attempts",
                )
                original_schema = {
                    table: self._schema_signature(engine, table)
                    for table in tracked_tables
                }
                original_rows = self._row_snapshot(engine)
                parent_checks = self._check_snapshot(engine)
                target_checks = self._expected_target_checks(parent_checks)
                self._migrate(config, url, TARGET_REVISION)
                self.assertEqual(self._check_snapshot(engine), target_checks)
                self.assertEqual(
                    {table: self._schema_signature(engine, table) for table in tracked_tables},
                    original_schema,
                )
                self.assertEqual(self._row_snapshot(engine), original_rows)
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT score FROM job_matches WHERE id='match'"
                    )).scalar_one(), 95)
                    self.assertEqual(connection.execute(text(
                        "SELECT state FROM applications WHERE id='application'"
                    )).scalar_one(), "PREPARED")
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)

                self._migrate(config, url, PARENT_REVISION, downgrade=True)
                self.assertEqual(self._check_snapshot(engine), parent_checks)
                self.assertEqual(
                    {table: self._schema_signature(engine, table) for table in tracked_tables},
                    original_schema,
                )
                self.assertEqual(self._row_snapshot(engine), original_rows)
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT score FROM job_matches WHERE id='match'"
                    )).scalar_one(), 95)
                    self.assertEqual(connection.execute(text(
                        "SELECT state FROM applications WHERE id='application'"
                    )).scalar_one(), "PREPARED")
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), PARENT_REVISION)

                self._migrate(config, url, TARGET_REVISION)
                self.assertEqual(self._check_snapshot(engine), target_checks)
                self.assertEqual(
                    {table: self._schema_signature(engine, table) for table in tracked_tables},
                    original_schema,
                )
                self.assertEqual(self._row_snapshot(engine), original_rows)
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)
            finally:
                engine.dispose()

    def test_invalid_existing_rows_abort_before_ddl_and_can_retry(self):
        unknown_state = "PRIVATE_RAW_STATE_MUST_NOT_LEAK"
        with tempfile.TemporaryDirectory(prefix="mjobs-constraint-preflight-") as directory:
            url, config, engine = self._legacy_database(
                directory,
                "invalid.db",
                score=101,
                state=unknown_state,
            )
            try:
                parent_checks = self._check_snapshot(engine)
                target_checks = self._expected_target_checks(parent_checks)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "1 invalid job match score row.*1 invalid application state row",
                ) as raised:
                    self._migrate(config, url, TARGET_REVISION)
                self.assertNotIn(unknown_state, str(raised.exception))
                self.assertEqual(self._check_snapshot(engine), parent_checks)
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), PARENT_REVISION)
                    self.assertEqual(connection.execute(text(
                        "SELECT score FROM job_matches WHERE id='match'"
                    )).scalar_one(), 101)
                    self.assertEqual(connection.execute(text(
                        "SELECT state FROM applications WHERE id='application'"
                    )).scalar_one(), unknown_state)

                with engine.begin() as connection:
                    connection.execute(text("UPDATE job_matches SET score=100 WHERE id='match'"))
                    connection.execute(text(
                        "UPDATE applications SET state='REVIEW_REQUIRED' WHERE id='application'"
                    ))
                self._migrate(config, url, TARGET_REVISION)
                self.assertEqual(self._check_snapshot(engine), target_checks)
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)
            finally:
                engine.dispose()

    def test_batch_failures_roll_back_schema_rows_version_and_retry_cleanly(self):
        tracked_tables = (
            "job_matches",
            "applications",
            "application_route_choices",
            "documents",
            "application_attempts",
        )
        phases = (
            ("after-first-batch", "before_cursor_execute"),
            ("during-second-batch", "after_cursor_execute"),
        )
        for phase, event_name in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                    prefix=f"mjobs-constraint-{phase}-") as directory:
                url, config, engine = self._legacy_database(directory, "fault.db")
                original_schema = {
                    table: self._schema_signature(engine, table)
                    for table in tracked_tables
                }
                original_rows = self._row_snapshot(engine)
                parent_checks = self._check_snapshot(engine)
                target_checks = self._expected_target_checks(parent_checks)
                database = make_url(url).database
                injected = []

                def interrupt_second_batch(connection, _cursor, statement,
                                           _parameters, _context, _executemany):
                    normalized = (statement.upper().replace('"', "")
                                  .replace("[", "").replace("]", ""))
                    if (connection.engine.url.database == database
                            and normalized.lstrip().startswith(
                                "CREATE TABLE _ALEMBIC_TMP_APPLICATIONS")):
                        injected.append(statement)
                        raise RuntimeError(f"synthetic {phase} interruption")

                event.listen(Engine, event_name, interrupt_second_batch)
                try:
                    with self.assertRaisesRegex(RuntimeError, f"synthetic {phase} interruption"):
                        self._migrate(config, url, TARGET_REVISION)
                finally:
                    event.remove(Engine, event_name, interrupt_second_batch)
                    engine.dispose()
                self.assertEqual(len(injected), 1)

                fresh = create_engine(url, poolclass=NullPool)
                try:
                    table_names = inspect(fresh).get_table_names()
                    self.assertFalse(any(name.startswith("_alembic_tmp") for name in table_names))
                    self.assertEqual(self._check_snapshot(fresh), parent_checks)
                    self.assertEqual(
                        {table: self._schema_signature(fresh, table) for table in tracked_tables},
                        original_schema,
                    )
                    self.assertEqual(self._row_snapshot(fresh), original_rows)
                    with fresh.connect() as connection:
                        self.assertEqual(connection.execute(text(
                            "SELECT version_num FROM alembic_version"
                        )).scalar_one(), PARENT_REVISION)
                finally:
                    fresh.dispose()

                self._migrate(config, url, TARGET_REVISION)
                retried = create_engine(url, poolclass=NullPool)
                try:
                    self.assertEqual(self._check_snapshot(retried), target_checks)
                    self.assertFalse(any(
                        name.startswith("_alembic_tmp")
                        for name in inspect(retried).get_table_names()
                    ))
                    self.assertEqual(self._row_snapshot(retried), original_rows)
                    with retried.connect() as connection:
                        self.assertEqual(connection.execute(text(
                            "SELECT version_num FROM alembic_version"
                        )).scalar_one(), TARGET_REVISION)
                finally:
                    retried.dispose()


if __name__ == "__main__":
    unittest.main()
