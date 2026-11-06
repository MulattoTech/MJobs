"""Synthetic resume-version outcome observations retain exact reported lineage."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.tracking.resume_outcomes import (
    analyze_resume_outcomes,
    record_observed_outcome,
    report_submitted_resume_version,
)
from tests.integration import test_document_generation_audit as fixture


class ResumeOutcomeObservationTests(unittest.TestCase):
    setUp = fixture.DocumentGenerationAuditTests.setUp
    _client = fixture.DocumentGenerationAuditTests._client
    tearDown = fixture.DocumentGenerationAuditTests.tearDown

    def _new_store(self):
        """Exercise the deployed migration path, not metadata.create_all."""
        self.database_url = f"sqlite:///{(self.root / 'history.db').as_posix()}"
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", self.database_url.replace("%", "%%"))
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": self.database_url}):
            command.upgrade(config, "head")
        return Store(self.database_url)

    def resume_document(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "resume",
            )).mappings().one()

    def bind(self, request_id="bind-1"):
        return report_submitted_resume_version(
            self.store, "fixture", self.application_id,
            self.resume_document()["id"], "2026-11-01T10:00:00+00:00", request_id,
        )

    def test_exact_version_and_observations_survive_reprepare_and_restart(self):
        original = self.resume_document()
        bound = self.bind()
        self.assertEqual(bound["document_id"], original["id"])
        self.assertEqual(bound["document_version"], original["version"])
        self.assertEqual(self.bind(), bound)

        record_observed_outcome(self.store, "fixture", self.application_id,
                                "RESPONSE", "2026-11-02T10:00:00+00:00", "response-1")
        record_observed_outcome(self.store, "fixture", self.application_id,
                                "SCREEN", "2026-11-03T10:00:00+00:00", "screen-1")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(document_version=99))
            connection.execute(update(m.documents).where(
                m.documents.c.id == original["id"]).values(version=99))
            connection.execute(delete(m.documents).where(m.documents.c.id == original["id"]))

        before = analyze_resume_outcomes(self.store, "fixture",
                                         "2026-11-01T00:00:00+00:00",
                                         "2026-11-04T00:00:00+00:00")
        self.assertEqual(before["sample_size"], 1)
        self.assertEqual(before["versions"][0]["document_version"], original["version"])
        self.assertEqual(before["versions"][0]["counts"], {
            "response": 1, "screen": 1, "interview": 0, "outcome": 0,
        })
        self.assertEqual(before["versions"][0]["unknown"], {
            "response": 0, "screen": 0, "interview": 1, "outcome": 1,
        })
        self.assertEqual(before["totals"]["counts"], before["versions"][0]["counts"])
        self.assertEqual(before["totals"]["rates"], {
            "response": 1.0, "screen": 1.0, "interview": 0.0, "outcome": 0.0,
        })
        self.assertEqual(before["interpretation"], "OBSERVATIONAL")
        self.assertEqual(before["warnings"], ["SELECTION_BIAS", "SPARSE_SAMPLE"])
        self.assertEqual(before["versions"][0]["warnings"], ["SPARSE_SAMPLE"])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.store.bind_workspace_mode("FIXTURE")
        self.client = self._client()
        self.assertEqual(analyze_resume_outcomes(self.store, "fixture",
                         "2026-11-01T00:00:00+00:00", "2026-11-04T00:00:00+00:00"), before)

    def test_duplicate_outcome_is_idempotent_and_conflicts_on_changed_content(self):
        self.bind()
        first = record_observed_outcome(self.store, "fixture", self.application_id,
                                        "INTERVIEW", "2026-11-03T10:00:00+00:00", "event-1")
        self.assertEqual(record_observed_outcome(self.store, "fixture", self.application_id,
                         "INTERVIEW", "2026-11-03T10:00:00+00:00", "event-1"), first)
        with self.assertRaises(ValueError):
            record_observed_outcome(self.store, "fixture", self.application_id,
                                    "REJECTED", "2026-11-03T10:00:00+00:00", "event-1")
        with self.assertRaises(ValueError):
            report_submitted_resume_version(self.store, "fixture", self.application_id,
                                            self.resume_document()["id"],
                                            "2026-11-02T10:00:00+00:00", "bind-2")
        summary = analyze_resume_outcomes(self.store, "fixture")
        self.assertEqual(summary["versions"][0]["counts"]["interview"], 1)

    def test_foreign_profile_and_nonresume_document_cannot_be_bound_or_read(self):
        with self.store.engine.connect() as connection:
            cover_letter = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "cover-letter",
            )).mappings().one()
        for profile_id, document_id in (
            ("other", self.resume_document()["id"]),
            ("fixture", cover_letter["id"]),
        ):
            with self.assertRaises((LookupError, ValueError)):
                report_submitted_resume_version(self.store, profile_id,
                    self.application_id, document_id, "2026-11-01T10:00:00+00:00", "bad")
        self.bind()
        self.assertEqual(analyze_resume_outcomes(self.store, "other")["sample_size"], 0)
        with self.assertRaises((LookupError, ValueError)):
            record_observed_outcome(self.store, "other", self.application_id,
                                    "OFFER", "2026-11-03T10:00:00+00:00", "foreign")

    def test_unbound_and_invalid_observations_leave_no_history(self):
        with self.assertRaises(LookupError):
            record_observed_outcome(self.store, "fixture", self.application_id,
                                    "RESPONSE", "2026-11-02T10:00:00+00:00", "early")
        for kind, timestamp in (
            ("PROVIDER_CONFIRMED", "2026-11-02T10:00:00+00:00"),
            ("RESPONSE", "2026-11-02T10:00:00"),
        ):
            with self.assertRaises(ValueError):
                record_observed_outcome(self.store, "fixture", self.application_id,
                                        kind, timestamp, "invalid")
        self.assertEqual(analyze_resume_outcomes(self.store, "fixture")["sample_size"], 0)

    def test_windows_exclude_other_submissions_and_late_outcomes(self):
        self.bind()
        record_observed_outcome(self.store, "fixture", self.application_id,
                                "RESPONSE", "2026-11-05T10:00:00+00:00", "late")
        early = analyze_resume_outcomes(self.store, "fixture",
                                        "2026-11-01T00:00:00+00:00",
                                        "2026-11-04T00:00:00+00:00")
        self.assertEqual(early["versions"][0]["counts"]["response"], 0)
        self.assertEqual(early["versions"][0]["unknown"]["response"], 1)
        self.assertEqual(analyze_resume_outcomes(self.store, "fixture",
                         "2026-11-02T00:00:00+00:00")["sample_size"], 0)

    def test_api_binding_event_and_windowed_readback_are_observational(self):
        state_before = self.store.application(self.application_id)["state"]
        base = f"/profiles/fixture/applications/{self.application_id}/resume-outcomes"
        binding = {"document_id": self.resume_document()["id"],
                   "reported_at": "2026-11-01T10:00:00+00:00", "request_id": "binding-api"}
        created = self.client.post(base + "/binding", json=binding)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["document_id"], binding["document_id"])
        self.assertEqual(created.json()["attribution"], "USER_REPORTED")
        self.assertEqual(self.client.post(base + "/binding", json=binding).json(), created.json())

        event = {"kind": "RESPONSE", "observed_at": "2026-11-02T10:00:00+00:00",
                 "request_id": "response-api"}
        observed = self.client.post(base + "/events", json=event)
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["kind"], "RESPONSE")
        self.assertEqual(self.client.post(base + "/events", json=event).json(), observed.json())
        self.assertEqual(self.client.post(base + "/events", json={
            **event, "kind": "SCREEN",
        }).status_code, 409)
        result = self.client.get("/profiles/fixture/resume-outcomes", params={
            "since": "2026-11-01T00:00:00+00:00",
            "until": "2026-11-03T00:00:00+00:00",
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["sample_size"], 1)
        self.assertEqual(result.json()["totals"]["counts"]["response"], 1)
        self.assertEqual(result.json()["totals"]["unknown"]["interview"], 1)
        self.assertEqual(result.json()["interpretation"], "OBSERVATIONAL")
        self.assertEqual(self.store.application(self.application_id)["state"], state_before)

    def test_api_rejects_foreign_invalid_and_conflicting_observations(self):
        base = f"/profiles/fixture/applications/{self.application_id}/resume-outcomes"
        binding = {"document_id": self.resume_document()["id"],
                   "reported_at": "2026-11-01T10:00:00+00:00", "request_id": "binding-api"}
        before_binding = self.client.post(base + "/events", json={
            "kind": "RESPONSE", "observed_at": "2026-11-02T10:00:00+00:00",
            "request_id": "too-early",
        })
        self.assertEqual(before_binding.status_code, 404)
        foreign = self.client.post(base.replace("/fixture/", "/other/", 1) + "/binding",
                                   json=binding)
        self.assertEqual(foreign.status_code, 404)
        with self.store.engine.connect() as connection:
            cover_letter = connection.execute(select(m.documents.c.id).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "cover-letter",
            )).scalar_one()
        invalid_document = self.client.post(base + "/binding", json={
            **binding, "document_id": cover_letter,
        })
        self.assertEqual(invalid_document.status_code, 422)
        self.assertEqual(self.client.post(base + "/binding", json=binding).status_code, 200)
        conflict = self.client.post(base + "/binding", json={
            **binding, "reported_at": "2026-11-01T11:00:00+00:00",
        })
        self.assertEqual(conflict.status_code, 409)
        invalid_kind = self.client.post(base + "/events", json={
            "kind": "PROVIDER_CONFIRMED", "observed_at": "2026-11-02T10:00:00+00:00",
            "request_id": "invalid-kind",
        })
        self.assertEqual(invalid_kind.status_code, 422)
        invalid_time = self.client.get("/profiles/fixture/resume-outcomes",
                                       params={"since": "2026-11-01T00:00:00"})
        self.assertEqual(invalid_time.status_code, 422)
        invalid_request = self.client.post(base + "/events", json={
            "kind": "SCREEN", "observed_at": "2026-11-02T10:00:00+00:00",
            "request_id": " padded ",
        })
        self.assertEqual(invalid_request.status_code, 422)
        self.assertEqual(self.client.get("/profiles/other/resume-outcomes").json()["sample_size"], 0)
        self.assertEqual(self.client.get("/profiles/fixture/resume-outcomes").json()["sample_size"], 1)


class ResumeOutcomeMigrationTests(unittest.TestCase):
    def test_upgrade_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-resume-outcomes-schema-") as directory:
            url = f"sqlite:///{(Path(directory) / 'schema.db').as_posix()}"
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                command.upgrade(config, "head")
                store = Store(url)
                try:
                    inspector = inspect(store.engine)
                    self.assertTrue(inspector.has_table("resume_outcome_bindings"))
                    self.assertTrue(inspector.has_table("resume_outcome_events"))
                    command.downgrade(config, "20261104rv01")
                    self.assertFalse(inspect(store.engine).has_table("resume_outcome_bindings"))
                    self.assertTrue(inspect(store.engine).has_table("profile_job_views"))
                    command.upgrade(config, "head")
                    with store.engine.begin() as connection:
                        connection.execute(text("INSERT INTO career_profiles (id, name, email, version) "
                                                "VALUES ('synthetic', 'Synthetic', 'synthetic@example.invalid', 'v1')"))
                        connection.execute(text("INSERT INTO resume_outcome_bindings "
                            "(id, profile_id, application_id, document_id, document_version, "
                            "document_sha256, reported_at, request_id, created_at) VALUES "
                            "('binding-1', 'synthetic', 'application-1', 'document-1', 1, "
                            "'{}', '2026-11-01T00:00:00+00:00', 'report-1', '2026-11-01T00:00:00+00:00')"))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard resume outcome history"):
                        command.downgrade(config, "20261104rv01")
                    self.assertTrue(inspect(store.engine).has_table("resume_outcome_bindings"))
                finally:
                    store.engine.dispose()
