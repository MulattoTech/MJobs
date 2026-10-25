import tempfile
import unittest
from pathlib import Path

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.applications.multipage_state import (
    MultipageStateConflict,
    MultipageStateError,
    initialize_workday_multipage_state,
    load_multipage_state,
    transition_workday_multipage_state,
    transition_multipage_state,
)


def workday_observation(heading, *, path="apply", labels=None,
                       actions=("Save and Continue",), stale=False):
    return {
        "url": f"https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/{path}?session=discarded",
        "headings": [heading],
        "field_labels": labels if labels is not None else [heading],
        "actions": list(actions),
        "stale": stale,
    }


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

    def enable_workday_route(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.routes.update().where(m.routes.c.id == "route").values(
                provider="workday",
                url="https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply"))

    def start_workday(self):
        self.enable_workday_route()
        first = workday_observation("My Information")
        result = initialize_workday_multipage_state(
            self.store, "profile", "application", first, request_id="wd-init-v1")
        self.assertEqual(result["decision"], "INITIALIZED")
        return result["state"], first

    def workday_transition(self, prior_state, previous, current, action, request_id):
        return transition_workday_multipage_state(
            self.store, "profile", "application", prior_state, previous, current,
            action, request_id=request_id,
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

    def test_workday_confirmed_transition_persists_fingerprint_and_exact_retry_is_idempotent(self):
        first_state, first_observation = self.start_workday()
        second_observation = workday_observation("My Experience", labels=["Resume"])
        committed = self.workday_transition(
            first_state, first_observation, second_observation, "NEXT", "wd-next-v1")
        self.assertEqual(committed["decision"], "CONFIRMED")
        self.assertEqual(committed["state"]["current_page_id"], "my_experience")
        self.assertEqual(committed["state"]["version"], first_state["version"] + 1)
        self.assertEqual(committed["state"]["workday_navigation"]["current_fingerprint"],
                         committed["fingerprint"])
        self.assertEqual(len(committed["state"]["workday_navigation"]["visited_fingerprints"]), 2)
        event_count = self.store.count("audit_events")

        database_url = self.store.url
        self.store.engine.dispose()
        self.store = Store(database_url)
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), committed["state"])
        retried = self.workday_transition(
            first_state, first_observation, second_observation, "NEXT", "wd-next-v1")
        self.assertEqual(retried, committed)
        self.assertEqual(self.store.count("audit_events"), event_count)

        changed_retry = workday_observation("My Experience", labels=["Different landmark"])
        conflict = self.workday_transition(
            first_state, first_observation, changed_retry, "NEXT", "wd-next-v1")
        self.assertEqual(conflict["decision"], "MANUAL_REVIEW")
        self.assertEqual(conflict["reason"], "MULTIPAGE_REQUEST_ID_CONFLICT")
        self.assertEqual(self.store.count("audit_events"), event_count)

    def test_workday_stale_version_fails_closed_without_advancing(self):
        first_state, first_observation = self.start_workday()
        current = workday_observation("My Experience", labels=["Resume"])
        committed = self.workday_transition(
            first_state, first_observation, current, "NEXT", "wd-next-v1")
        self.assertEqual(committed["decision"], "CONFIRMED")
        before = load_multipage_state(self.store, "profile", "application")

        stale_retry = self.workday_transition(
            first_state, first_observation, current, "NEXT", "wd-stale-new-key")
        self.assertEqual(stale_retry["decision"], "MANUAL_REVIEW")
        self.assertEqual(stale_retry["reason"], "MULTIPAGE_VERSION_CONFLICT")
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), before)

    def test_workday_cross_tenant_observation_does_not_persist(self):
        first_state, first_observation = self.start_workday()
        before_count = self.store.count("audit_events")
        other_tenant = workday_observation("My Experience", labels=["Resume"])
        other_tenant["url"] = (
            "https://another.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply?session=discarded"
        )

        result = self.workday_transition(
            first_state, first_observation, other_tenant, "NEXT", "wd-cross-tenant")

        self.assertEqual(result["decision"], "MANUAL_REVIEW")
        self.assertEqual(result["reason"], "WORKDAY_ROUTE_MISMATCH")
        self.assertIsNone(result["state"])
        self.assertEqual(self.store.count("audit_events"), before_count)
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), first_state)

    def test_workday_initial_observation_must_match_application_route(self):
        self.enable_workday_route()
        other_tenant = workday_observation("My Information")
        other_tenant["url"] = (
            "https://another.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply?session=discarded"
        )

        result = initialize_workday_multipage_state(
            self.store, "profile", "application", other_tenant, request_id="wd-init-other-tenant")

        self.assertEqual(result["decision"], "MANUAL_REVIEW")
        self.assertEqual(result["reason"], "WORKDAY_ROUTE_MISMATCH")
        self.assertIsNone(result["state"])
        self.assertEqual(self.store.count("audit_events"), 0)

    def test_workday_unknown_loop_interruption_mismatch_and_submit_do_not_advance(self):
        first_state, first_observation = self.start_workday()
        before_count = self.store.count("audit_events")
        resumed = self.workday_transition(
            first_state, None, first_observation, "RESUME", "wd-resume-match")
        self.assertEqual(resumed["decision"], "RESUME_FROM_CONFIRMED_STEP")
        self.assertEqual(resumed["state"], first_state)
        self.assertEqual(self.store.count("audit_events"), before_count)
        unknown = workday_observation("Additional Details", labels=["Unknown question"])
        loop = workday_observation("My Information")
        changed_mismatch = workday_observation("Application Questions", labels=["Question"])
        stale = workday_observation("My Experience", labels=["Resume"], stale=True)
        resume_mismatch = workday_observation("My Experience", labels=["Resume"])
        cases = (
            (unknown, "NEXT", "wd-unknown", "MANUAL_REVIEW"),
            (loop, "NEXT", "wd-loop", "MANUAL_REVIEW"),
            (changed_mismatch, "NEXT", "wd-changed-mismatch", "MANUAL_REVIEW"),
            (stale, "NEXT", "wd-stale-observation", "REFRESH_AND_RECLASSIFY"),
            (resume_mismatch, "RESUME", "wd-resume-mismatch", "MANUAL_REVIEW"),
            (resume_mismatch, "FINAL_SUBMIT", "wd-final-submit", "MANUAL_REVIEW"),
        )
        for observed, action, request_id, expected_decision in cases:
            with self.subTest(action=action):
                result = self.workday_transition(
                    first_state, first_observation, observed, action, request_id)
                self.assertEqual(result["decision"], expected_decision)
                self.assertIsNone(result.get("state"))
        self.assertEqual(self.store.count("audit_events"), before_count)
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), first_state)

    def test_workday_refresh_decisions_are_preserved_without_advancing(self):
        state, observation = self.start_workday()
        before_count = self.store.count("audit_events")
        explicit_refresh = self.workday_transition(
            state, None, observation, "REFRESH", "wd-refresh-explicit")
        self.assertEqual(explicit_refresh, {
            "decision": "REFRESH_AND_RECLASSIFY",
            "reason": "REFRESH_REQUIRES_NEW_OBSERVATION",
            "execute": False,
            "state": None,
        })

        stale = workday_observation("My Information", stale=True)
        stale_refresh = self.workday_transition(
            state, observation, stale, "NEXT", "wd-refresh-stale")
        self.assertEqual(stale_refresh["decision"], "REFRESH_AND_RECLASSIFY")
        self.assertEqual(stale_refresh["reason"], "STALE_OBSERVATION")
        self.assertIsNone(stale_refresh["state"])
        self.assertEqual(self.store.count("audit_events"), before_count)
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), state)

    def test_workday_final_page_confirmation_uses_review_action(self):
        state, observation = self.start_workday()
        for index, heading in enumerate(("My Experience", "Application Questions", "Voluntary Disclosures"), 1):
            next_observation = workday_observation(heading)
            result = self.workday_transition(
                state, observation, next_observation, "NEXT", f"wd-next-{index}")
            self.assertEqual(result["decision"], "CONFIRMED")
            state, observation = result["state"], next_observation

        review_observation = workday_observation("Review", actions=("Submit",))
        result = self.workday_transition(
            state, observation, review_observation, "NEXT", "wd-review")
        self.assertEqual(result["decision"], "CONFIRMED")
        self.assertEqual((result["state"]["current_page_id"], result["state"]["status"]),
                         ("review", "REVIEW"))

    def test_generic_lever_state_path_remains_versioned_and_supported(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.routes.update().where(m.routes.c.id == "route").values(
                provider="lever", url="https://jobs.lever.co/example/req"))
        first = self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"])
        second = self.transition("NEXT", first["version"], "contact", next_page_id="eligibility")
        self.assertEqual((second["current_page_id"], second["version"]), ("eligibility", 2))

    def test_generic_greenhouse_state_path_remains_versioned_and_supported(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.routes.update().where(m.routes.c.id == "route").values(
                provider="greenhouse", url="https://boards.greenhouse.io/example/jobs/123"))
        first = self.transition("SAVE_PROGRESS", 0, "contact", page_order=["contact", "eligibility"])
        second = self.transition("NEXT", first["version"], "contact", next_page_id="eligibility")
        self.assertEqual((second["current_page_id"], second["version"]), ("eligibility", 2))

    def test_workday_generic_transition_without_observation_evidence_is_rejected(self):
        self.enable_workday_route()
        with self.assertRaises(MultipageStateError) as error:
            self.transition("SAVE_PROGRESS", 0, "my_information",
                            page_order=["my_information", "my_experience"])
        self.assertEqual(error.exception.code, "WORKDAY_OBSERVATION_REQUIRED")
        self.assertEqual(self.store.count("audit_events"), 0)

    def test_workday_raw_multipage_request_cannot_bypass_navigation_confirmation(self):
        self.enable_workday_route()
        with self.assertRaises(MultipageStateError) as error:
            transition_multipage_state(self.store, "profile", "application", {
                "request_id": "wd-unconfirmed-direct",
                "action": "SAVE_PROGRESS",
                "expected_version": 0,
                "page_id": "my_information",
                "page_order": ["my_information", "my_experience", "application_questions",
                               "voluntary_disclosures"],
                "progress": {},
                "workday_navigation": {
                    "expected_fingerprint": None,
                    "current_fingerprint": "a" * 64,
                    "visited_fingerprints": ["a" * 64],
                    "application_origin": "https://acme.wd5.myworkdayjobs.com",
                },
            })
        self.assertEqual(error.exception.code, "WORKDAY_CONFIRMATION_REQUIRED")
        self.assertEqual(self.store.count("audit_events"), 0)


if __name__ == "__main__":
    unittest.main()
