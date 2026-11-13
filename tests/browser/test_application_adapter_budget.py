"""Real Chromium regressions for budgeted, read-only ATS form analysis."""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from services.applications.adapters import breezy, teamtailor
from services.applications.adapters.breezy import BreezyApplicationAdapter
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter
from workers.operations import OperationTimedOut


class _OperationBudget:
    """Use the same callable/remaining-seconds contract as the worker."""

    def __init__(self, seconds=5):
        self.deadline = time.monotonic() + seconds
        self.checks = 0

    def __call__(self):
        self.checks += 1
        if time.monotonic() >= self.deadline:
            raise OperationTimedOut("synthetic fixture budget expired")

    def remaining_seconds(self):
        self()
        return self.deadline - time.monotonic()


class ApplicationAdapterBudgetBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.requests = []
        self.page.route("**/*", self._refuse_request)

    def _refuse_request(self, route):
        self.requests.append(route.request.url)
        route.abort()

    def _contact_form(self):
        self.page.set_content("""<!doctype html><html><body>
            <form id="application">
                <input name="email" id="email" type="email" aria-label="Email"
                       value="private-fixture@example.invalid">
            </form>
            <script>
                window.fixtureActions = {input: 0, change: 0, submit: 0};
                for (const kind of Object.keys(window.fixtureActions)) {
                    document.querySelector('form').addEventListener(kind, event => {
                        window.fixtureActions[kind]++;
                        if (kind === 'submit') event.preventDefault();
                    });
                }
            </script>
        </body></html>""")

    def _assert_budgeted_contact_analysis(self, adapter):
        self._contact_form()
        # Establish that the fixture is supported before adding the worker budget.
        ordinary = adapter.analyze_page(self.page)
        self.assertEqual(ordinary["status"], "PASS")
        self.assertFalse(adapter.requires_human_review(ordinary))

        budget = _OperationBudget()
        analysis = adapter.analyze_page(self.page, check=budget)
        self.assertEqual(analysis["status"], "PASS")
        self.assertEqual(len(analysis["fields"]), 1)
        self.assertIs(analysis["fields"][0].get("user_visible"), True)
        self.assertIs(analysis["fields"][0].get("user_editable"), True)
        self.assertFalse(adapter.requires_human_review(analysis))
        self.assertNotIn("private-fixture@example.invalid", json.dumps(analysis))
        self.assertGreater(budget.checks, 2)
        self.assertEqual(self.page.locator("#email").input_value(),
                         "private-fixture@example.invalid")
        self.assertEqual(self.page.evaluate("window.fixtureActions"),
                         {"input": 0, "change": 0, "submit": 0})
        self.assertEqual(self.requests, [])

    def test_teamtailor_contact_analysis_accepts_worker_budget(self):
        self._assert_budgeted_contact_analysis(TeamtailorApplicationAdapter())

    def test_breezy_contact_analysis_accepts_worker_budget(self):
        self._assert_budgeted_contact_analysis(BreezyApplicationAdapter())

    def test_budgeted_noninteractive_controls_still_require_review(self):
        cases = (
            ("hidden", 'style="display:none"'),
            ("invisible", 'style="visibility:hidden"'),
            ("disabled", "disabled"),
            ("readonly", "readonly"),
            ("clipped", 'style="clip-path:inset(50%)"'),
        )
        for adapter in (TeamtailorApplicationAdapter(), BreezyApplicationAdapter()):
            for name, attributes in cases:
                with self.subTest(provider=adapter.provider, case=name):
                    self.page.set_content(
                        '<form><input name="email" type="email" aria-label="Email" '
                        'value="private-fixture@example.invalid" ' + attributes + '></form>'
                    )
                    analysis = adapter.analyze_page(self.page, check=_OperationBudget())
                    self.assertTrue(adapter.requires_human_review(analysis))
                    self.assertNotIn("private-fixture@example.invalid", json.dumps(analysis))
                    self.assertEqual(self.page.locator("input").input_value(),
                                     "private-fixture@example.invalid")
        self.assertEqual(self.requests, [])

    def test_budgeted_breezy_occluded_control_still_requires_review(self):
        self._contact_form()
        self.page.evaluate("""() => {
            const rect = document.querySelector('input').getBoundingClientRect();
            const cover = document.createElement('div');
            Object.assign(cover.style, {
                position: 'fixed', zIndex: '100',
                left: rect.left + 'px', top: rect.top + 'px',
                width: rect.width + 'px', height: rect.height + 'px',
                background: 'white'
            });
            document.body.appendChild(cover);
        }""")
        adapter = BreezyApplicationAdapter()
        analysis = adapter.analyze_page(self.page, check=_OperationBudget())
        self.assertTrue(adapter.requires_human_review(analysis))
        self.assertNotIn("private-fixture@example.invalid", json.dumps(analysis))
        self.assertEqual(self.page.evaluate("window.fixtureActions"),
                         {"input": 0, "change": 0, "submit": 0})
        self.assertEqual(self.requests, [])

    def test_budgeted_missing_or_multiple_forms_still_require_review(self):
        for adapter in (TeamtailorApplicationAdapter(), BreezyApplicationAdapter()):
            for markup in (
                '<input name="email" type="email" aria-label="Email">',
                '<form><input name="email" type="email"></form>'
                '<form><input name="email" type="email"></form>',
            ):
                with self.subTest(provider=adapter.provider, markup=markup):
                    self.page.set_content(markup)
                    analysis = adapter.analyze_page(self.page, check=_OperationBudget())
                    self.assertEqual(analysis["status"], "UNKNOWN")
                    self.assertTrue(adapter.requires_human_review(analysis))
        self.assertEqual(self.requests, [])

    def _assert_changed_form_is_reviewed(self, mutation):
        for module, adapter in (
            (teamtailor, TeamtailorApplicationAdapter()),
            (breezy, BreezyApplicationAdapter()),
        ):
            with self.subTest(provider=adapter.provider):
                self._contact_form()
                original = module.analyze_form_page

                def change_after_metadata(page, *, check=None):
                    analysis = original(page, check=check)
                    self.assertEqual(analysis["status"], "PASS")
                    # Change the real DOM between metadata and rendered-state reads.
                    page.evaluate(mutation)
                    return analysis

                with patch.object(module, "analyze_form_page",
                                  side_effect=change_after_metadata):
                    analysis = adapter.analyze_page(self.page, check=_OperationBudget())
                self.assertTrue(adapter.requires_human_review(analysis))
                self.assertFalse(any(field.get("user_visible") is True
                                     for field in analysis.get("fields", [])))
                self.assertNotIn("private-fixture@example.invalid", json.dumps(analysis))
                self.assertEqual(self.page.evaluate("window.fixtureActions"),
                                 {"input": 0, "change": 0, "submit": 0})
        self.assertEqual(self.requests, [])

    def test_budgeted_form_multiplication_after_metadata_still_requires_review(self):
        self._assert_changed_form_is_reviewed("""() => {
            document.body.appendChild(document.querySelector('form').cloneNode(true));
        }""")

    def test_budgeted_control_count_change_after_metadata_still_requires_review(self):
        self._assert_changed_form_is_reviewed("""() => {
            const added = document.createElement('input');
            added.name = 'additional_email';
            added.type = 'email';
            added.value = 'private-fixture@example.invalid';
            document.querySelector('form').appendChild(added);
        }""")
