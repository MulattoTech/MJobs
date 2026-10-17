"""Connected synthetic Workday checkpoint and interruption recovery contract."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.application_multipage_state import create_application_multipage_state_router
from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.multipage_state import (
    initialize_workday_multipage_state,
    transition_workday_multipage_state,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


def observation(heading, *, host="wd5.myworkdayjobs.com", stale=False):
    return {
        "url": f"https://{host}/acme/job/REQ-1/apply?session=synthetic-only",
        "headings": [heading],
        "field_labels": [heading],
        "actions": ["Save and Continue"],
        "stale": stale,
    }


class FleetT03WorkdayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t03-workday-")
        self.root = Path(self.temp.name)
        self.database = self.root / "workday.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        with self.store.engine.begin() as connection:
            application = connection.execute(m.applications.select().where(
                m.applications.c.id == self.application_id,
            )).mappings().one()
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(
                provider="workday",
                url="https://wd5.myworkdayjobs.com/acme/job/REQ-1/apply",
            ))
        self.url = f"/profiles/fixture/applications/{self.application_id}/multipage-state"
        self.client = self._client()

    def _client(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        app.include_router(create_application_multipage_state_router(self.store))
        return TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_checkpoint_survives_api_readback_restart_and_interrupted_resume(self):
        first = observation("My Information")
        initialized = initialize_workday_multipage_state(
            self.store, "fixture", self.application_id, first, request_id="t03-init-v1",
        )
        self.assertEqual(initialized["decision"], "INITIALIZED")
        self.assertEqual(initialized["state"]["version"], 1)

        advanced = transition_workday_multipage_state(
            self.store, "fixture", self.application_id, initialized["state"], first,
            observation("My Experience"), "NEXT", request_id="t03-next-v2",
        )
        self.assertEqual(advanced["decision"], "CONFIRMED")
        expected = advanced["state"]
        self.assertEqual(expected["current_page_id"], "my_experience")
        self.assertEqual(expected["version"], 2)

        readback = self.client.get(self.url)
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json(), expected)
        self.assertEqual(readback.json()["workday_navigation"]["visited_fingerprints"], [
            initialized["state"]["workday_navigation"]["current_fingerprint"],
            advanced["fingerprint"],
        ])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = self._client()
        restarted = self.client.get(self.url)
        self.assertEqual(restarted.status_code, 200, restarted.text)
        self.assertEqual(restarted.json(), expected)

        resumed = transition_workday_multipage_state(
            self.store, "fixture", self.application_id, restarted.json(),
            observation("My Experience"), observation("My Experience"), "RESUME",
            request_id="t03-resume-v3",
        )
        self.assertEqual(resumed["decision"], "RESUME_FROM_CONFIRMED_STEP")
        self.assertEqual(resumed["state"], expected)
        self.assertEqual(self.client.get(self.url).json(), expected)

    def test_stale_observation_and_changed_route_fail_without_checkpoint_mutation(self):
        first = observation("My Information")
        initialized = initialize_workday_multipage_state(
            self.store, "fixture", self.application_id, first, request_id="t03-init-stale",
        )
        before = initialized["state"]

        stale = transition_workday_multipage_state(
            self.store, "fixture", self.application_id, before, first,
            observation("My Experience", stale=True), "NEXT", request_id="t03-stale-next",
        )
        self.assertEqual(stale["decision"], "REFRESH_AND_RECLASSIFY")
        self.assertEqual(stale["reason"], "STALE_OBSERVATION")
        self.assertEqual(self.client.get(self.url).json(), before)

        other_tenant = observation("My Experience", host="other.myworkdayjobs.com")
        mismatch = transition_workday_multipage_state(
            self.store, "fixture", self.application_id, before, first, other_tenant,
            "NEXT", request_id="t03-wrong-tenant",
        )
        self.assertEqual(mismatch["decision"], "MANUAL_REVIEW")
        self.assertEqual(mismatch["reason"], "WORKDAY_ROUTE_MISMATCH")
        self.assertEqual(self.client.get(self.url).json(), before)

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url="https://other.myworkdayjobs.com/acme/job/REQ-1/apply"))
        changed_route = transition_workday_multipage_state(
            self.store, "fixture", self.application_id, before, first,
            observation("My Information"), "RESUME", request_id="t03-changed-route",
        )
        self.assertEqual(changed_route["decision"], "MANUAL_REVIEW")
        self.assertEqual(changed_route["reason"], "WORKDAY_ROUTE_MISMATCH")
        self.assertEqual(self.client.get(self.url).json(), before)


if __name__ == "__main__":
    unittest.main()
