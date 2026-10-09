import tempfile
import unittest
from pathlib import Path

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.applications.multipage_state import (
    MultipageStateConflict,
    MultipageStateError,
    load_multipage_state,
    transition_multipage_state,
)


class MultipageApplicationStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        with self.store.engine.begin() as connection:
            upsert(connection, m.profiles, {
                "id": "profile", "name": "Local User", "email": "user@example.test",
                "version": "profile-v1",
            })
            upsert(connection, m.companies, {
                "id": "company", "name": "Example", "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": "job", "company_id": "company", "requisition": "REQ-1",
                "title": "Example Role", "description": "Example", "skills": [],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "route", "job_id": "job", "company_id": "company",
                "provider": "mock", "tenant": "example", "requisition": "REQ-1",
                "url": "http://127.0.0.1/apply",
            })
            upsert(connection, m.applications, {
                "id": "application", "job_id": "job", "route_id": "route",
                "profile_id": "profile", "profile_version": "profile-v1",
                "state": "PREPARED", "target": "MOCK", "fact_ids": [], "trace": {},
                "created_at": "2026-10-08T00:00:00+00:00", "submitted_at": None,
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def transition(self, action, version, page_id, **extra):
        extra.setdefault("request_id", f"request-{version}-{action.lower()}")
        extra.setdefault("progress", {})
        return transition_multipage_state(
            self.store, "profile", "application",
            {"action": action, "expected_version": version, "page_id": page_id, **extra},
        )

    def test_state_survives_reload_and_supports_next_previous_and_review(self):
        first = self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"],
                                progress={"contact": {"city": "Tulsa"}})
        self.assertEqual((first["current_page_id"], first["version"]), ("contact", 1))
        self.assertTrue(first["manual_intervention_required"])
        self.store.engine.dispose()
        reopened_store = Store(self.store.url)
        self.assertEqual(load_multipage_state(reopened_store, "profile", "application"), first)
        reopened_store.engine.dispose()

        second = self.transition("NEXT", 1, "contact", next_page_id="eligibility",
                                 progress={"contact": {"city": "Tulsa"}})
        self.assertEqual((second["current_page_id"], second["version"]), ("eligibility", 2))
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), second)

        back = self.transition("PREVIOUS", 2, "eligibility", previous_page_id="contact",
                               progress={"contact": {"city": "Tulsa"}})
        self.assertEqual((back["current_page_id"], back["version"]), ("contact", 3))
        forward = self.transition("NEXT", 3, "contact", next_page_id="eligibility",
                                  progress={"contact": {"city": "Tulsa"}})
        review = self.transition("REVIEW", 4, "eligibility", progress={"contact": {"city": "Tulsa"}})
        self.assertEqual((review["current_page_id"], review["status"]), ("review", "REVIEW"))
        self.assertEqual(review["progress"], forward["progress"])
        returned = self.transition("PREVIOUS", 5, "review", previous_page_id="eligibility",
                                   progress={"contact": {"city": "Tulsa"}})
        self.assertEqual((returned["current_page_id"], returned["status"]),
                         ("eligibility", "IN_PROGRESS"))

    def test_stale_version_is_rejected_without_writing_an_event(self):
        self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"],
                        progress={"contact": {"city": "Tulsa"}})
        before = self.store.count("audit_events")

        with self.assertRaises(MultipageStateConflict):
            self.transition("NEXT", 0, "contact", next_page_id="eligibility",
                            progress={"contact": {"city": "Tulsa"}})

        self.assertEqual(self.store.count("audit_events"), before)
        self.assertEqual(load_multipage_state(self.store, "profile", "application")["version"], 1)

    def test_invalid_previous_destination_is_rejected_without_state_change(self):
        self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"], progress={})
        self.transition("NEXT", 1, "contact", next_page_id="eligibility", progress={})
        before = load_multipage_state(self.store, "profile", "application")

        with self.assertRaises(MultipageStateError):
            self.transition("PREVIOUS", 2, "eligibility", previous_page_id="review", progress={})

        self.assertEqual(load_multipage_state(self.store, "profile", "application"), before)

    def test_unknown_page_progress_and_nonadjacent_navigation_are_atomic_rejections(self):
        self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"],
                        progress={"contact": {"city": "Tulsa"}})
        before_state = load_multipage_state(self.store, "profile", "application")
        before_event_count = self.store.count("audit_events")

        with self.assertRaises(MultipageStateError) as progress_error:
            self.transition("SAVE_PROGRESS", 1, "contact",
                            progress={"unknown-page": {"answer": "value"}})
        self.assertEqual(progress_error.exception.code, "INVALID_PROGRESS")
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), before_state)
        self.assertEqual(self.store.count("audit_events"), before_event_count)

        with self.assertRaises(MultipageStateError) as navigation_error:
            self.transition("NEXT", 1, "contact", next_page_id="unknown-page",
                            progress={"contact": {"city": "Tulsa"}})
        self.assertEqual(navigation_error.exception.code, "INVALID_TRANSITION")
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), before_state)
        self.assertEqual(self.store.count("audit_events"), before_event_count)

    def test_exact_retry_returns_committed_state_and_conflicting_retry_is_rejected(self):
        self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"],
                        progress={"contact": {"city": "Tulsa"}})
        request = {"request_id": "retryable-next", "action": "NEXT", "expected_version": 1,
                   "page_id": "contact", "next_page_id": "eligibility",
                   "progress": {"contact": {"city": "Tulsa"}}}
        committed = transition_multipage_state(self.store, "profile", "application", request)
        committed_event_count = self.store.count("audit_events")

        database_url = self.store.url
        self.store.engine.dispose()
        self.store = Store(database_url)
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), committed)
        retried = transition_multipage_state(self.store, "profile", "application", request)
        self.assertEqual(retried, committed)
        self.assertEqual(retried["version"], committed["version"])
        self.assertEqual(self.store.count("audit_events"), committed_event_count)

        with self.assertRaises(MultipageStateConflict) as error:
            transition_multipage_state(self.store, "profile", "application", {
                **request, "progress": {"contact": {"city": "Oklahoma City"}},
            })
        self.assertEqual(error.exception.code, "MULTIPAGE_REQUEST_ID_CONFLICT")
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), committed)
        self.assertEqual(self.store.count("audit_events"), committed_event_count)

    def test_review_does_not_allow_navigation_or_submission(self):
        self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact"], progress={})
        review = self.transition("REVIEW", 1, "contact", progress={})
        self.assertTrue(review["manual_intervention_required"])

        with self.assertRaises(MultipageStateError):
            self.transition("NEXT", 2, "review", next_page_id="submit", progress={})
        with self.assertRaises(MultipageStateError):
            self.transition("SUBMIT", 2, "review", progress={})
        with self.store.engine.connect() as connection:
            application = connection.execute(m.applications.select().where(
                m.applications.c.id == "application")).mappings().one()
        self.assertEqual((application["state"], application["submitted_at"]), ("PREPARED", None))

    def test_other_profiles_cannot_read_or_update_application_page_state(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.profiles, {
                "id": "other-profile", "name": "Other", "email": "other@example.test",
                "version": "other-v1",
            })

        with self.assertRaises(MultipageStateError) as error:
            load_multipage_state(self.store, "other-profile", "application")
        self.assertEqual(error.exception.code, "APPLICATION_NOT_FOUND")

    def test_credential_fields_are_rejected_before_the_state_is_persisted(self):
        with self.assertRaises(MultipageStateError) as error:
            self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact"],
                            progress={"contact": {"password": "private-value"}})
        self.assertEqual(error.exception.code, "INVALID_PROGRESS")
        self.assertEqual(self.store.count("audit_events"), 0)


if __name__ == "__main__":
    unittest.main()
