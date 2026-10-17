"""Synthetic contract tests for the inspection-only Breezy adapter."""
import unittest
from pathlib import Path
from unittest.mock import patch

from services.applications.adapters.breezy import BreezyApplicationAdapter


class BreezyApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BreezyApplicationAdapter()

    def test_normalizes_public_job_and_apply_routes(self):
        route = self.adapter.normalize_route({
            "provider": "BREEZY",
            "url": "https://Acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-Software-Engineer/apply?source=fixture",
        })
        self.assertEqual(route, {
            "provider": "breezy",
            "tenant": "acme",
            "requisition": "123e4567-e89b-12d3-a456-426614174000",
            "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-software-engineer",
        })

    def test_tenant_metadata_is_ascii_and_keeps_ascii_case_variants(self):
        url = "https://k.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"
        self.assertEqual(self.adapter.normalize_route({
            "provider": "breezy", "url": url, "tenant": "K",
        })["tenant"], "k")
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "breezy", "url": url, "tenant": "K",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "breezy",
            "url": "https://K.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job",
        }))

    def test_job_slug_rejects_unicode_casefold_lookalikes(self):
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "breezy",
            "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-ſoftware-engineer",
        }))

    def test_rejects_foreign_malformed_and_ambiguous_routes(self):
        invalid = (
            None,
            {"provider": "lever", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "http://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr.attacker.test/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://user@acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr:443/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/not-a-position"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job/unknown"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job/"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job#apply"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job", "tenant": "other"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job", "requisition": "other"},
        )
        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_capabilities_are_inspection_only(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_known_basic_contact_fixture_needs_no_extra_review(self):
        analysis = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "email", "sensitivity": "STANDARD", "ambiguous": False,
            "labels": ["Email address"], "user_visible": True, "user_editable": True,
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_analyze_page_uses_shared_accessible_label_and_attaches_dom_state(self):
        controls = [{
            "tag": "input", "labels": [], "name": "email", "id": "email",
            "placeholder": None, "aria_label": "Email address", "input_type": "email",
            "nearby_text": None, "section_heading": None, "fieldset_legend": None,
            "unsafe_text_context": False, "options": [],
        }]

        class Controls:
            def evaluate_all(self, script):
                return [{
                    "user_visible": True, "hit_test_visible": True, "user_editable": True,
                    "paint_styles": [{
                        "clip_path": "none", "clip": "auto", "contain": "none",
                        "opacity": "1",
                    }],
                    "control_rect": {"left": 10, "top": 10, "right": 20, "bottom": 20},
                    "overflow_clip_regions": [], "clip_geometry_transformed": False,
                }]

        class Form:
            @property
            def first(self):
                return self

            def count(self):
                return 1

            def evaluate(self, script, **kwargs):
                return controls

            def locator(self, selector):
                return Controls()

        class Page:
            def locator(self, selector):
                return Form()

        result = self.adapter.analyze_page(Page())

        self.assertEqual(result["fields"][0]["labels"], [])
        self.assertEqual(result["fields"][0]["aria_label"], "Email address")
        self.assertIs(result["fields"][0]["user_visible"], True)
        self.assertIs(result["fields"][0]["user_editable"], True)
        self.assertFalse(self.adapter.requires_human_review(result))

    def test_analyze_page_rejects_clipped_and_nearly_transparent_controls(self):
        base_state = {
            "user_visible": True, "hit_test_visible": True, "user_editable": True,
            "paint_styles": [{
                "clip_path": "none", "clip": "auto", "contain": "none", "opacity": "1",
            }],
            "control_rect": {"left": 10, "top": 10, "right": 20, "bottom": 20},
            "overflow_clip_regions": [], "clip_geometry_transformed": False,
        }
        states = (
            ("clip-path", {**base_state, "paint_styles": [{
                **base_state["paint_styles"][0], "clip_path": "inset(50%)",
            }]}),
            ("overflow clip", {**base_state, "overflow_clip_regions": [{
                "clips_horizontally": True, "clips_vertically": False,
                "left": 30, "top": 0, "right": 40, "bottom": 30,
            }]}),
            ("transformed overflow geometry", {
                **base_state,
                "overflow_clip_regions": [{
                    "clips_horizontally": True, "clips_vertically": False,
                    "left": 0, "top": 0, "right": 30, "bottom": 30,
                }],
                "clip_geometry_transformed": True,
            }),
            ("near-zero opacity", {**base_state, "paint_styles": [{
                **base_state["paint_styles"][0], "opacity": "0.001",
            }]}),
            ("near-zero ancestor opacity", {**base_state, "paint_styles": [
                base_state["paint_styles"][0],
                {**base_state["paint_styles"][0], "opacity": "0.001"},
            ]}),
            ("cumulative ancestor opacity", {**base_state, "paint_styles": [
                {**base_state["paint_styles"][0], "opacity": "0.2"},
                {**base_state["paint_styles"][0], "opacity": "0.2"},
            ]}),
        )

        for name, state in states:
            with self.subTest(hidden_state=name):
                controls = [{
                    "tag": "input", "labels": [], "name": "email", "id": "email",
                    "placeholder": None, "aria_label": "Email address", "input_type": "email",
                    "nearby_text": None, "section_heading": None, "fieldset_legend": None,
                    "unsafe_text_context": False, "options": [],
                }]

                class ControlLocator:
                    def evaluate_all(self, script):
                        return [state]

                class Form:
                    @property
                    def first(self):
                        return self

                    def count(self):
                        return 1

                    def evaluate(self, script, **kwargs):
                        return controls

                    def locator(self, selector):
                        return ControlLocator()

                class Page:
                    def locator(self, selector):
                        return Form()

                result = self.adapter.analyze_page(Page())
                self.assertTrue(self.adapter.requires_human_review(result))

    def test_real_dom_projection_rejects_hidden_clipped_and_occluded_controls(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable")

        fixtures = (
            ("visible", '<input type="email" aria-label="Email address">', "", False),
            ("hidden", '<input type="email" aria-label="Email address" style="display:none">', "", True),
            ("disabled", '<input type="email" aria-label="Email address" disabled>', "", True),
            ("readonly", '<input type="email" aria-label="Email address" readonly>', "", True),
            ("clip-path", '<input type="email" aria-label="Email address" style="clip-path:inset(50%)">', "", True),
            ("near-transparent", '<input type="email" aria-label="Email address" style="opacity:0.001">', "", True),
            ("overflow-clipped", '<div class="clip"><input type="email" aria-label="Email address"></div>',
             ".clip{position:absolute;left:20px;top:20px;width:30px;height:30px;overflow:hidden}"
             ".clip input{position:absolute;left:40px;top:0;width:120px;height:30px}", True),
            ("overlay", '<input type="email" aria-label="Email address">'
             '<div class="overlay"></div>',
             ".overlay{position:fixed;inset:0;background:white;z-index:9999}", True),
        )

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                candidates = (
                    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                    Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser"),
                    Path("/usr/bin/google-chrome"),
                )
                executable = next((candidate for candidate in candidates
                                   if candidate.is_file()), None)
            if executable is None or not executable.is_file():
                self.skipTest("No local Chromium-compatible browser is installed")
            browser = playwright.chromium.launch(
                headless=True, executable_path=str(executable),
            )
            try:
                page = browser.new_page(viewport={"width": 640, "height": 480})
                for name, control, css, expected_review in fixtures:
                    with self.subTest(dom_state=name):
                        page.set_content(
                            f"<style>{css}</style><form>{control}</form>"
                        )
                        analysis = self.adapter.analyze_page(page)
                        self.assertEqual(analysis["status"], "PASS")
                        self.assertEqual(
                            self.adapter.requires_human_review(analysis), expected_review,
                        )
            finally:
                browser.close()

    def test_analyze_page_missing_dom_evidence_requires_review(self):
        analysis = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "email", "sensitivity": "STANDARD",
            "ambiguous": False, "labels": ["Email address"],
        }]}

        class Controls:
            def evaluate_all(self, script):
                return []

        class Form:
            @property
            def first(self):
                return self

            def count(self):
                return 1

            def locator(self, selector):
                return Controls()

        class Page:
            def locator(self, selector):
                return Form()

        with patch("services.applications.adapters.breezy.analyze_form_page",
                   return_value=analysis):
            result = self.adapter.analyze_page(Page())

        self.assertTrue(self.adapter.requires_human_review(result))

    def test_controls_without_positive_visible_editable_evidence_require_review(self):
        known_contact = {
            "tag": "input", "input_type": "email", "sensitivity": "STANDARD",
            "ambiguous": False, "labels": ["Email address"],
        }
        fields = (
            ("missing evidence", known_contact),
            ("hidden", {**known_contact, "user_visible": False,
                         "user_editable": False, "hidden": True}),
            ("disabled", {**known_contact, "user_visible": True,
                           "user_editable": False, "disabled": True}),
            ("readonly", {**known_contact, "user_visible": True,
                           "user_editable": False, "readonly": True}),
        )
        for state, field in fields:
            with self.subTest(state=state):
                self.assertTrue(self.adapter.requires_human_review({
                    "status": "PASS", "fields": [field],
                }))

    def test_select_and_textarea_controls_require_review_even_with_contact_labels(self):
        for tag in ("select", "textarea"):
            with self.subTest(tag=tag):
                analysis = {"status": "PASS", "fields": [{
                    "tag": tag, "input_type": None, "sensitivity": "STANDARD",
                    "ambiguous": False, "labels": ["Email address"],
                }]}
                self.assertTrue(self.adapter.requires_human_review(analysis))

    def test_missing_or_conflicting_prompt_metadata_requires_review(self):
        fields = (
            {"tag": "input", "input_type": "text", "sensitivity": "STANDARD",
             "ambiguous": False, "labels": [], "name": "first_name"},
            {"tag": "input", "input_type": "text", "sensitivity": "STANDARD",
             "ambiguous": False, "labels": [], "name": "first_name",
             "placeholder": "Why do you want this job?"},
            {"tag": "input", "input_type": "email", "sensitivity": "STANDARD",
             "ambiguous": False, "labels": [], "id": "email",
             "aria_label": "Tell us about yourself"},
            {"tag": "input", "input_type": "email", "sensitivity": "STANDARD",
             "ambiguous": False, "labels": ["Email address"],
             "placeholder": "Why do you want to work here?"},
            {"tag": "input", "input_type": "email", "sensitivity": "STANDARD",
             "ambiguous": False, "labels": ["Email address"],
             "aria_label": "Why do you want this job?"},
        )
        for field in fields:
            with self.subTest(field=field):
                self.assertTrue(self.adapter.requires_human_review({
                    "status": "PASS", "fields": [field],
                }))

    def test_unknown_sensitive_ambiguous_and_unsupported_controls_fail_closed(self):
        analyses = (
            None,
            {"status": "UNKNOWN", "fields": []},
            {"status": "PASS", "fields": [{
                "input_type": "text", "sensitivity": "STANDARD", "ambiguous": False,
                "labels": ["Why do you want to work here?"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "password", "sensitivity": "CREDENTIAL", "ambiguous": False,
                "labels": ["Email address"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "email", "sensitivity": "STANDARD", "ambiguous": True,
                "labels": ["Email address"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "checkbox", "sensitivity": "STANDARD", "ambiguous": False,
                "labels": ["Email address"],
            }]},
        )
        for analysis in analyses:
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))


if __name__ == "__main__":
    unittest.main()
