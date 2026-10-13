"""Mounted consent -> durable outbox -> fresh worker -> synthetic browser/reload."""
import copy
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update

from packages.persistence import models as m
from services.applications.attempt_history import read_attempt_history
from tests.browser import test_mock_run_controls as flow


class ConditionalControlIdentityTests(unittest.TestCase):
    def test_replaced_control_cannot_inherit_inspected_identity(self):
        from services.applications.mock_adapter import _visible_form_controls
        from services.applications.mock_dynamic_review import observed_controls
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                for mutation in ["node.name = 'unreviewed'", "node.id = 'replacement'",
                                 "node.required = false", "node.multiple = true", "node.disabled = true",
                                 "node.style.display = 'none'", "node.setAttribute('data-mjobs-dynamic-field', '')"]:
                    with self.subTest(mutation=mutation):
                        page.set_content('<form><label for="region">State or province</label>'
                            '<select id="region" name="region" required data-mjobs-question-id="region">'
                            '<option value="">Choose</option><option>Oklahoma</option></select></form>')
                        inspected = _visible_form_controls(page)
                        page.locator('select').evaluate('node => { ' + mutation + '; }')
                        self.assertEqual(observed_controls(page, inspected), [])
                        self.assertEqual(page.locator('select').input_value(), '')
            finally:
                browser.close()


class MockDynamicReviewBrowserTests(flow.MockRunBrowserHarness):
    def setUp(self):
        profile = copy.deepcopy(flow.PROFILE)
        profile["answers"].append({"id": "state", "question": "State or province", "text": "Oklahoma",
            "source": "fictional-test-profile", "verified": True, "user_approved": True, "confidence": 100})
        with patch.object(flow, "PROFILE", profile):
            super().setUp()
        with self.store.engine.begin() as connection:
            app = self.store.application(self.app_id)
            route = connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(
                url=f"{self.fixture}/ats/apply/{route['requisition']}?dynamic=known"))
        self.open_application()

    def test_review_lost_reply_reload_new_worker_and_replay_never_submit(self):
        self.panel.get_by_role("button", name="Run dry-run", exact=True).click()
        expect(self.panel).to_contain_text("QUEUED")
        first = self.operations()[0]["id"]
        self.run_worker(first, expected_status="BLOCKED", refresh_panel=False)
        self.panel.get_by_role("button", name="Refresh run status", exact=True).click()
        expect(self.panel).to_contain_text("BLOCKED")
        self.assertEqual(self.store.application(self.app_id)["state"], "REVIEW_REQUIRED")
        review = self.page.get_by_role("region", name="Conditional dry-run review", exact=True)
        expect(review.get_by_text("Oklahoma", exact=True)).to_be_visible()
        save = review.get_by_role("button", name="Save conditional-field review", exact=True)
        expect(save).to_be_disabled()
        endpoint = f"{self.origin}/profiles/fixture/applications/{self.app_id}/mock-dynamic-review"
        committed = []
        def lose_reply(route):
            if route.request.method == "POST":
                response = route.fetch()
                self.assertEqual(response.status, 200)
                committed.append(response.json())
                route.abort("failed")
            else:
                route.continue_()
        self.page.route(endpoint, lose_reply)
        review.get_by_role("checkbox").check()
        save.click()
        expect(review).to_contain_text("Conditional review saved")
        self.page.unroute(endpoint, lose_reply)
        self.assertEqual(len(committed), 1)
        self.open_application()
        expect(review).to_contain_text("Conditional review saved")
        expect(review.get_by_role("checkbox")).not_to_be_checked()
        self.run_worker(first, expected_status="ALREADY_CLAIMED_OR_CAPACITY", refresh_panel=False)
        self.assertEqual(len(read_attempt_history(self.store, "fixture", self.app_id)["attempts"]), 1)
        self.panel.get_by_role("button", name="Start a new reviewed run", exact=True).click()
        self.panel.get_by_role("button", name="Run dry-run", exact=True).click()
        expect(self.panel).to_contain_text("QUEUED")
        second = next(row["id"] for row in self.operations() if row["id"] != first)
        self.run_worker(second)
        self.assertEqual(self.store.application(self.app_id)["state"], "DRY_RUN_COMPLETE")
        self.run_worker(second, expected_status="ALREADY_CLAIMED_OR_CAPACITY", refresh_panel=False)
        self.open_application()
        expect(self.panel).to_contain_text("COMPLETED")
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})
        self.assertEqual(len(read_attempt_history(self.store, "fixture", self.app_id)["attempts"]), 2)
        self.assertIsNone(self.store.application(self.app_id)["confirmation"])
        self.assertFalse(self.external)
