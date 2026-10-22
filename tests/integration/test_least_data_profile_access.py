"""Least-data profile lookup is task-scoped, auditable, and value-redacting."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from packages.persistence import models as m
from packages.persistence.store import Store
from services.career_profile.profile_access import (
    ProfileAccessDenied,
    _eligible_fact,
    build_profile_context,
)
from tests.integration.test_spine import PROFILE


class LeastDataProfileAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_cover_letter_context_contains_only_requested_approved_fact_and_minimal_identity(self):
        operation_id = str(uuid4())
        result = build_profile_context(
            self.store, "fixture", "cover_letter", operation_id,
            ["name", "facts.python.text", "contact"],
        )

        self.assertEqual(result["context"]["name"], PROFILE["name"])
        self.assertIn("Python", result["context"]["facts.python.text"])
        self.assertEqual(result["context"]["contact"]["type"], "object")
        self.assertIsInstance(result["context"]["contact"]["preview"], list)
        serialized = str(result["context"])
        self.assertNotIn(PROFILE["email"], serialized)
        self.assertNotIn("preferences", serialized)
        self.assertEqual(result["accessed_fact_ids"], ["python"])

        with self.store.engine.connect() as connection:
            event = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_ACCESSED")
            ).mappings().one()
        self.assertEqual(event["payload"]["operation_id"], operation_id)
        self.assertEqual(event["payload"]["task"], "cover_letter")
        self.assertEqual(event["payload"]["fact_ids"], ["python"])
        self.assertNotIn(PROFILE["facts"][0]["text"], str(event["payload"]))

    def test_unapproved_task_cannot_read_even_an_existing_fact(self):
        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(
                self.store, "fixture", "debug_dump", str(uuid4()),
                ["facts.python.text"],
            )

        with self.store.engine.connect() as connection:
            denied = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_DENIED")
            ).mappings().one()
        self.assertEqual(denied["payload"]["task"], "UNAPPROVED")
        self.assertNotIn(PROFILE["facts"][0]["text"], str(denied["payload"]))

    def test_profile_context_accepts_confidence_bounds_inclusively(self):
        version = self.store.profile("fixture")["version"]
        for confidence in (80.0, 100.0):
            with self.subTest(confidence=confidence):
                with self.store.engine.begin() as connection:
                    connection.execute(m.facts.update().where(
                        m.facts.c.profile_id == "fixture",
                        m.facts.c.version == version,
                        m.facts.c.fact_id == "python",
                    ).values(confidence=confidence))

                result = build_profile_context(
                    self.store, "fixture", "resume_tailor", str(uuid4()),
                    ["facts.python.text"],
                )
                self.assertIn("Python", result["context"]["facts.python.text"])

    def test_profile_fact_gate_rejects_nan_confidence(self):
        # SQLite stores NaN as NULL, so exercise the access predicate directly.
        fact = dict(PROFILE["facts"][0], confidence=float("nan"))
        self.assertFalse(_eligible_fact(fact))

    def test_profile_context_rejects_corrupt_nonfinite_or_out_of_range_fact_confidence(self):
        version = self.store.profile("fixture")["version"]
        for confidence in (float("inf"), float("-inf"), 79.99, 100.01):
            with self.subTest(confidence=confidence):
                with self.store.engine.begin() as connection:
                    connection.execute(m.facts.update().where(
                        m.facts.c.profile_id == "fixture",
                        m.facts.c.version == version,
                        m.facts.c.fact_id == "python",
                    ).values(confidence=confidence))

                with self.assertRaises(ProfileAccessDenied):
                    build_profile_context(
                        self.store, "fixture", "resume_tailor", str(uuid4()),
                        ["facts.python.text"],
                    )

                with self.store.engine.begin() as connection:
                    connection.execute(m.facts.update().where(
                        m.facts.c.profile_id == "fixture",
                        m.facts.c.version == version,
                        m.facts.c.fact_id == "python",
                    ).values(confidence=100.0))

    def test_task_denylist_wins_over_an_otherwise_approved_path(self):
        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(
                self.store, "fixture", "cover_letter", str(uuid4()),
                ["facts.python.text"], deny_paths=["facts.python"],
            )

    def test_unverified_fact_is_not_returned_or_audited_as_accessed(self):
        fact = dict(PROFILE["facts"][0], id="pending", verified=False,
                    user_approved=False, text="Pending synthetic fact")
        profile = dict(PROFILE, facts=[fact])
        self.store.save_profile(profile)

        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(
                self.store, "fixture", "resume_tailor", str(uuid4()),
                ["facts.pending.text"],
            )

    def test_parent_container_preview_never_returns_child_values(self):
        result = build_profile_context(
            self.store, "fixture", "resume_tailor", str(uuid4()), ["facts"],
        )

        facts = result["context"]["facts"]
        self.assertEqual(facts["type"], "array")
        self.assertEqual(facts["count"], len(PROFILE["facts"]))
        self.assertNotIn(PROFILE["facts"][0]["text"], str(facts))

    def test_profile_snapshot_access_is_isolated_by_profile_id(self):
        other_fact = dict(PROFILE["facts"][0], id="private-other", text="Other profile private synthetic fact")
        self.store.save_profile(dict(PROFILE, id="other", email="other@example.test", facts=[other_fact]))

        first = build_profile_context(self.store, "fixture", "resume_tailor", str(uuid4()),
                                      ["facts.python.text"])
        second = build_profile_context(self.store, "other", "resume_tailor", str(uuid4()),
                                       ["facts.private-other.text"])

        self.assertNotIn("Other profile private synthetic fact", str(first["context"]))
        self.assertNotIn("Python", str(second["context"]))
        self.assertIn("Other profile private synthetic fact", str(second["context"]))

    def test_invalid_dotted_path_fails_closed_and_does_not_log_path_value(self):
        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(self.store, "fixture", "resume_tailor", str(uuid4()),
                                  ["facts..text"])

        with self.store.engine.connect() as connection:
            denied = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_DENIED")
            ).mappings().one()
        self.assertEqual(denied["payload"]["reason"], "INVALID_PATH")
        self.assertEqual(denied["payload"]["paths"], [])

    def test_denied_path_is_not_persisted_to_the_audit_log(self):
        private_path = "contact.patient_condition_private"
        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(self.store, "fixture", "resume_tailor", str(uuid4()),
                                  [private_path])

        with self.store.engine.connect() as connection:
            denied = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_DENIED")
            ).mappings().one()
        self.assertEqual(denied["payload"]["paths"], [])
        self.assertNotIn(private_path, str(denied["payload"]))

    def test_malformed_task_and_unhashable_path_fail_as_access_denials(self):
        for task, path in (([], "facts.python.text"),
                           ("resume_tailor", ["facts.python.text"])):
            with self.subTest(task=task, path=path):
                with self.assertRaises(ProfileAccessDenied):
                    build_profile_context(self.store, "fixture", task, str(uuid4()), [path])

    def test_oversized_profile_value_is_rejected_without_silent_truncation(self):
        large = dict(PROFILE["facts"][0], id="large", text="x" * 4001)
        self.store.save_profile(dict(PROFILE, facts=[large]))

        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(self.store, "fixture", "resume_tailor", str(uuid4()),
                                  ["facts.large.text"])

    def test_aggregate_context_limit_fails_without_truncating_facts(self):
        facts = [dict(PROFILE["facts"][0], id=f"long-{index}", text="x" * 3900)
                 for index in range(9)]
        self.store.save_profile(dict(PROFILE, facts=facts))
        paths = [f"facts.long-{index}.text" for index in range(9)]

        with self.assertRaises(ProfileAccessDenied):
            build_profile_context(self.store, "fixture", "resume_tailor", str(uuid4()), paths)

    def test_access_audit_survives_store_reopen_without_storing_profile_values(self):
        operation_id = str(uuid4())
        build_profile_context(self.store, "fixture", "resume_tailor", operation_id,
                              ["facts.python.text"])
        database_url = self.store.url
        self.store.engine.dispose()
        reopened = Store(database_url)
        try:
            with reopened.engine.connect() as connection:
                event = connection.execute(
                    m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_ACCESSED")
                ).mappings().one()
            self.assertEqual(event["payload"]["operation_id"], operation_id)
            self.assertEqual(event["payload"]["fact_ids"], ["python"])
            self.assertNotIn(PROFILE["facts"][0]["text"], str(event["payload"]))
        finally:
            reopened.engine.dispose()
