"""Salary matching receives only its explicit preference leaf."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from packages.persistence import models as m
from packages.persistence.store import Store
from services.career_profile.profile_access import ProfileAccessDenied, build_profile_context
from tests.integration.test_spine import PROFILE


class JobMatchSalaryProfileAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'profile.db').as_posix()}")
        self.store.migrate()
        self.sensitive_salary = "USD 98765-123456 annually private-synthetic"
        self.store.save_profile({
            **PROFILE,
            "preferences": {
                **PROFILE.get("preferences", {}),
                "salary_expectations": self.sensitive_salary,
                "sponsorship_needs": "NOT_REQUIRED",
            },
        })

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_job_match_can_read_only_salary_expectation_and_audit_never_logs_value(self):
        operation_id = str(uuid4())
        result = build_profile_context(
            self.store, "fixture", "job_match", operation_id,
            ["preferences.salary_expectations"],
        )

        self.assertEqual(result["context"], {
            "preferences.salary_expectations": self.sensitive_salary,
        })
        with self.store.engine.connect() as connection:
            event = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_ACCESSED")
            ).mappings().one()
        self.assertEqual(event["payload"]["paths"], ["preferences.salary_expectations"])
        self.assertNotIn(self.sensitive_salary, str(event["payload"]))

    def test_job_match_cannot_read_other_preference_leaves(self):
        for path in ("preferences", "preferences.sponsorship_needs",
                     "preferences.career_goals", "preferences.work_authorization"):
            with self.subTest(path=path), self.assertRaises(ProfileAccessDenied):
                build_profile_context(
                    self.store, "fixture", "job_match", str(uuid4()), [path],
                )

        with self.store.engine.connect() as connection:
            events = connection.execute(
                m.audit.select().where(m.audit.c.action == "AI_PROFILE_CONTEXT_DENIED")
            ).mappings().all()
        self.assertTrue(events)
        self.assertTrue(all(event["payload"]["paths"] == [] for event in events))
        self.assertNotIn(self.sensitive_salary, str(events))


if __name__ == "__main__":
    unittest.main()
