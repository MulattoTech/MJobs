"""Synthetic checkpoint data persistence and interruption guards for Team 3."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.applications.multipage_state import (
    initialize_workday_multipage_state,
    load_multipage_state,
    transition_workday_multipage_state,
)
from services.applications.adapters.workday_navigation import workday_origin
from services.applications import multipage_state


def observation(heading):
    return {
        "url": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply?session=synthetic",
        "headings": [heading],
        "field_labels": [heading],
        "actions": ["Save and Continue"],
        "stale": False,
    }


class TestFleetT03WorkdayProgress(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t03-progress-")
        self.root = Path(self.temp.name)
        self.database = self.root / "multipage.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        with self.store.engine.begin() as connection:
            upsert(connection, m.profiles, {
                "id": "profile", "name": "Synthetic User", "email": "synthetic@example.invalid",
                "version": "v1",
            })
            upsert(connection, m.companies, {
                "id": "company", "name": "Synthetic", "domain": "example.invalid",
            })
            upsert(connection, m.jobs, {
                "id": "job", "company_id": "company", "requisition": "REQ-1",
                "title": "Synthetic Role", "description": "Synthetic", "skills": [],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "route", "job_id": "job", "company_id": "company",
                "provider": "workday", "tenant": "acme", "requisition": "REQ-1",
                "url": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply",
            })
            upsert(connection, m.applications, {
                "id": "application", "job_id": "job", "route_id": "route",
                "profile_id": "profile", "profile_version": "v1", "state": "PREPARED",
                "target": "MOCK", "fact_ids": [], "trace": {},
                "created_at": "2026-10-17T00:00:00+00:00", "submitted_at": None,
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_current_observation_saves_progress_and_reopens_without_stale_write(self):
        first = observation("My Information")
        initialized = initialize_workday_multipage_state(
            self.store, "profile", "application", first, request_id="wd-init",
        )
        self.assertEqual(initialized["decision"], "INITIALIZED")
        checkpoint = initialized["state"]

        saved = transition_workday_multipage_state(
            self.store, "profile", "application", checkpoint, first, first,
            "SAVE_PROGRESS", request_id="wd-save",
            progress={"my_information": {"city": "Synthetic City"}},
        )
        self.assertEqual(saved["decision"], "CONFIRMED")
        checkpoint = saved["state"]
        self.assertEqual(checkpoint["version"], 2)
        self.assertEqual(checkpoint["progress"]["my_information"]["city"], "Synthetic City")
        audit_count = self.store.count("audit_events")

        stale = transition_workday_multipage_state(
            self.store, "profile", "application", checkpoint, first,
            observation("My Experience"), "SAVE_PROGRESS", request_id="wd-stale-save",
            progress={"my_information": {"city": "Must Not Persist"}},
        )
        self.assertEqual(stale["decision"], "MANUAL_REVIEW")
        self.assertIsNone(stale["state"])
        secret = transition_workday_multipage_state(
            self.store, "profile", "application", checkpoint, first, first,
            "SAVE_PROGRESS", request_id="wd-secret-save",
            progress={"my_information": {"password": "synthetic-only"}},
        )
        self.assertEqual(secret["decision"], "MANUAL_REVIEW")
        self.assertEqual(self.store.count("audit_events"), audit_count)

        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.assertEqual(load_multipage_state(self.store, "profile", "application"), checkpoint)

    def test_previous_pops_active_fingerprint_path_and_resume_allows_next(self):
        information = observation("My Information")
        experience = observation("My Experience")
        initialized = initialize_workday_multipage_state(
            self.store, "profile", "application", information, request_id="wd-init-roundtrip",
        )
        self.assertEqual(initialized["decision"], "INITIALIZED")
        moved_forward = transition_workday_multipage_state(
            self.store, "profile", "application", initialized["state"], information,
            experience, "NEXT", request_id="wd-next-experience",
        )
        self.assertEqual(moved_forward["decision"], "CONFIRMED")

        returned = transition_workday_multipage_state(
            self.store, "profile", "application", moved_forward["state"], experience,
            information, "PREVIOUS", request_id="wd-previous-information",
        )
        self.assertEqual(returned["decision"], "CONFIRMED")
        self.assertEqual(returned["state"]["current_page_id"], "my_information")
        self.assertEqual(returned["state"]["workday_navigation"]["visited_fingerprints"],
                         [initialized["state"]["workday_navigation"]["current_fingerprint"]])

        audit_count = self.store.count("audit_events")
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        durable = load_multipage_state(self.store, "profile", "application")
        self.assertEqual(durable, returned["state"])
        resumed = transition_workday_multipage_state(
            self.store, "profile", "application", durable, None, information,
            "RESUME", request_id="wd-resume-information",
        )
        self.assertEqual(resumed["decision"], "RESUME_FROM_CONFIRMED_STEP")
        self.assertEqual(self.store.count("audit_events"), audit_count)

        forward_again = transition_workday_multipage_state(
            self.store, "profile", "application", resumed["state"], information,
            experience, "NEXT", request_id="wd-next-experience-again",
        )
        self.assertEqual(forward_again["decision"], "CONFIRMED")
        self.assertEqual(forward_again["state"]["current_page_id"], "my_experience")
        self.assertEqual(len(forward_again["state"]["workday_navigation"]["visited_fingerprints"]), 2)

    def test_bound_checkpoint_pins_route_id_and_workspace_mode_through_resume(self):
        def route_digest(url):
            parsed = urlsplit(url)
            identity = json.dumps(
                ["workday-route-v1", workday_origin(url), parsed.path],
                separators=(",", ":"), ensure_ascii=True,
            )
            return hashlib.sha256(identity.encode("utf-8")).hexdigest()

        with self.store.engine.begin() as connection:
            upsert(connection, m.workspace_identity, {
                "id": "workspace", "operation_mode": "FIXTURE",
                "bound_at": "2026-10-17T00:00:00+00:00",
            })
        first = observation("My Information")
        with patch.object(multipage_state, "workday_route_digest", route_digest):
            initialized = initialize_workday_multipage_state(
                self.store, "profile", "application", first, request_id="wd-init-bound",
            )
            self.assertEqual(initialized["decision"], "INITIALIZED")
            checkpoint = initialized["state"]
            navigation = checkpoint["workday_navigation"]
            self.assertEqual(navigation["workspace_mode"], "FIXTURE")
            self.assertEqual(navigation["route_id"], "route")
            self.assertEqual(navigation["route_digest"], route_digest(first["url"]))

            resumed = transition_workday_multipage_state(
                self.store, "profile", "application", checkpoint, first, first,
                "RESUME", request_id="wd-resume-bound",
            )
            self.assertEqual(resumed["decision"], "RESUME_FROM_CONFIRMED_STEP")

            with self.store.engine.begin() as connection:
                upsert(connection, m.routes, {
                    "id": "route-2", "job_id": "job", "company_id": "company",
                    "provider": "workday", "tenant": "other", "requisition": "REQ-2",
                    "url": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply",
                })
                connection.execute(m.applications.update().where(
                    m.applications.c.id == "application",
                ).values(route_id="route-2"))
            replaced_route = transition_workday_multipage_state(
                self.store, "profile", "application", checkpoint, first, first,
                "RESUME", request_id="wd-resume-replaced-route",
            )
            self.assertEqual(replaced_route["reason"], "WORKDAY_ROUTE_MISMATCH")
            self.assertIsNone(replaced_route["state"])

            with self.store.engine.begin() as connection:
                connection.execute(m.workspace_identity.update().where(
                    m.workspace_identity.c.id == "workspace",
                ).values(operation_mode="PERSONAL"))
            changed_mode = transition_workday_multipage_state(
                self.store, "profile", "application", checkpoint, first, first,
                "RESUME", request_id="wd-resume-changed-mode",
            )
            self.assertEqual(changed_mode["reason"], "WORKSPACE_MODE_MISMATCH")
            self.assertIsNone(changed_mode["state"])


if __name__ == "__main__":
    unittest.main()
