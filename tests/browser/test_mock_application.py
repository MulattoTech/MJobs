from pathlib import Path
import tempfile
import unittest

import httpx
from sqlalchemy import update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover, MockClient
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class MockApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def prepared(self, origin, profile=None):
        if profile is not None:
            self.store.save_profile(profile)
        discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs() if row["requisition"] == "B")
        return prepare(self.store, job["id"], "fixture", self.root)

    def test_dry_run_fills_two_pages_uploads_and_stops_before_submit(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            self.assertIn("UPLOAD_RESUME", result["trace"])
            self.assertIn("VALIDATE", result["trace"])
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(), {"status": "not_found"})

    def test_mock_submission_confirmed_once_and_survives_restart(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "CONFIRMED")
            self.assertIn("VERIFY_CONFIRMATION", result["trace"])
            reopened = Store(self.store.url)
            try:
                again = apply_mock(reopened, app_id, origin, self.root, submit=True)
                self.assertEqual(again["state"], "CONFIRMED")
                self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json()["submission_count"], 1)
            finally:
                reopened.engine.dispose()

    def test_timeout_after_submission_reservation_prevents_submit_click(self):
        from workers.operations import OperationTimedOut
        with fixture_server() as origin:
            app_id = self.prepared(origin)

            def expire_at_reserved_submit_boundary():
                if self.store.application(app_id)['state'] == 'SUBMITTING':
                    raise OperationTimedOut('OPERATION_TIMEOUT')

            with self.assertRaises(OperationTimedOut):
                apply_mock(self.store, app_id, origin, self.root, submit=True,
                           check=expire_at_reserved_submit_boundary)

            result = self.store.application(app_id)
            self.assertEqual(result['state'], 'SUBMISSION_UNCONFIRMED')
            self.assertEqual(httpx.get(origin + '/ats/receipts/' + app_id).json(),
                             {'status': 'not_found'})

    def test_uncertain_submission_without_receipt_does_not_retry(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            self.store.transition(app_id, ["PREPARED"], "SUBMITTING")
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(), {"status": "not_found"})

    def test_captcha_requires_intervention(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(m.routes.c.id == self.store.application(app_id)["route_id"]).values(url=origin + "/ats/apply/B?challenge=true"))
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "USER_INTERVENTION_REQUIRED")

    def test_nonlocal_mock_origin_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            MockClient("https://example.com")
        with self.assertRaises(ValueError):
            MockClient("http://mock")

    def test_receipt_network_failure_is_persisted_as_uncertain(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            self.store.transition(app_id, ["PREPARED"], "SUBMITTING")
            from unittest.mock import patch
            with patch.object(MockClient, "get", side_effect=httpx.ConnectError("fixture receipt unavailable")):
                result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")

    def test_hourly_limit_counts_submission_time_for_old_prepared_application(self):
        from services.applications.mock_adapter import _authorize
        from packages.domain.safety import Policy
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(m.applications.c.id == app_id).values(created_at="2000-01-01T00:00:00+00:00"))
            self.assertEqual(apply_mock(self.store, app_id, origin, self.root, submit=True)["state"], "CONFIRMED")
            other_job = next(row for row in self.store.jobs() if row["requisition"] == "A")
            other = prepare(self.store, other_job["id"], "fixture", self.root)
            self.store.transition(other, ["PREPARED"], "READY_TO_SUBMIT")
            decision = _authorize(self.store, other, 100, Policy(hourly_limit=1))
            self.assertFalse(decision.allowed)
            self.assertIn("HOURLY_LIMIT_REACHED", decision.reasons)

    def test_changed_profile_does_not_change_prepared_contact_or_answers(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            old_version = self.store.application(app_id)["profile_version"]
            self.store.save_profile(dict(PROFILE, name="Changed Fixture", email="changed@example.test", answers=[]))
            self.assertEqual(self.store.profile("fixture", old_version)["name"], "Alex Fixture")
            result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")

    def test_missing_required_answer_stops_before_browser_and_shows_question(self):
        from unittest.mock import patch
        profile = dict(PROFILE, answers=[row for row in PROFILE["answers"] if row["id"] != "remote"])
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            with patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertFalse(browser.called)
            from services.applications.preflight import latest_preflight
            report = latest_preflight(self.store, app_id)
            self.assertEqual(report["result"], "FAIL")
            self.assertEqual(report["missing"][0]["question_id"], "remote")
            self.assertEqual(report["missing"][0]["answer_ids"], ["remote"])

    def test_changed_questionnaire_stops_before_browser_as_stale(self):
        from unittest.mock import patch
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            initial = httpx.get(origin + "/ats/questions/B").json()
            changed = dict(initial, revision=2)
            from services.applications.preflight import latest_preflight
            with patch.object(MockClient, "get", side_effect=[httpx.Response(200, json=initial),
                                                                httpx.Response(200, json=changed)]), \
                    patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertFalse(browser.called)
            report = latest_preflight(self.store, app_id)
            self.assertEqual(report["result"], "UNKNOWN")
            self.assertEqual(report["reason"], "STALE_PREFLIGHT")


if __name__ == "__main__":
    unittest.main()
