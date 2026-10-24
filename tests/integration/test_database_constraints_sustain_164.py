"""Sustained SQLite enforcement checks for the core relational spine."""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


TIMESTAMP = "2026-10-24T12:00:00Z"
PARENT_REVISION = "20261018cm01"
TARGET_REVISION = "20261024dc01"
APPLICATION_STATES = (
    "DRAFT",
    "PREPARED",
    "FILLING",
    "AUTH_REQUIRED",
    "USER_INTERVENTION_REQUIRED",
    "READY_TO_SUBMIT",
    "DRY_RUN_COMPLETE",
    "REVIEW_REQUIRED",
    "SUBMITTING",
    "SUBMISSION_UNCONFIRMED",
    "CONFIRMED",
    "EXTERNALLY_APPLIED",
    "FAILED",
    "CANCELLED",
    "REJECTED",
    "BLOCKED",
    "DEAD_LETTERED",
    "DISCARDED",
    "UNKNOWN",
)


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
        return url, config, engine

    @staticmethod
    def _check_names(engine, table):
        return {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints(table)
        }

    def test_upgrade_downgrade_and_reupgrade_preserve_valid_rows(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-constraint-migration-") as directory:
            url, config, engine = self._legacy_database(directory, "valid.db")
            try:
                self._migrate(config, url, TARGET_REVISION)
                self.assertIn("ck_job_matches_score_range", self._check_names(engine, "job_matches"))
                self.assertIn("ck_applications_state_valid", self._check_names(engine, "applications"))
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
                self.assertNotIn("ck_job_matches_score_range", self._check_names(engine, "job_matches"))
                self.assertNotIn("ck_applications_state_valid", self._check_names(engine, "applications"))
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT score FROM job_matches WHERE id='match'"
                    )).scalar_one(), 95)
                    self.assertEqual(connection.execute(text(
                        "SELECT state FROM applications WHERE id='application'"
                    )).scalar_one(), "PREPARED")

                self._migrate(config, url, TARGET_REVISION)
                self.assertIn("ck_job_matches_score_range", self._check_names(engine, "job_matches"))
                self.assertIn("ck_applications_state_valid", self._check_names(engine, "applications"))
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
                with self.assertRaisesRegex(
                    RuntimeError,
                    "1 invalid job match score row.*1 invalid application state row",
                ) as raised:
                    self._migrate(config, url, TARGET_REVISION)
                self.assertNotIn(unknown_state, str(raised.exception))
                self.assertNotIn("ck_job_matches_score_range", self._check_names(engine, "job_matches"))
                self.assertNotIn("ck_applications_state_valid", self._check_names(engine, "applications"))
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
                self.assertIn("ck_job_matches_score_range", self._check_names(engine, "job_matches"))
                self.assertIn("ck_applications_state_valid", self._check_names(engine, "applications"))
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
