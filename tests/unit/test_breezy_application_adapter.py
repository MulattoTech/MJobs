"""Synthetic contract tests for the inspection-only Breezy adapter."""
import unittest
from pathlib import Path
from unittest.mock import patch

from services.applications.adapters.breezy import (
    BreezyApplicationAdapter,
    _CONTACT_PROMPTS,
    _CONTROL_STATE_SCRIPT,
    _SAFE_LABEL_METADATA_SCRIPT,
)


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

            def evaluate(self, script, arg=None, **kwargs):
                return controls

            def locator(self, selector):
                return Controls()

        class Page:
            def locator(self, selector):
                return Form()

        result = self.adapter.analyze_page(Page())

        self.assertEqual(result["fields"][0]["labels"], [])
        self.assertEqual(result["fields"][0]["aria_label"], "email address")
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
            ("disjoint nested overflow clips", {
                **base_state,
                "control_rect": {"left": 20, "top": 10, "right": 80, "bottom": 20},
                "overflow_clip_regions": [
                    {"clips_horizontally": True, "clips_vertically": False,
                     "left": 0, "top": 0, "right": 30, "bottom": 30},
                    {"clips_horizontally": True, "clips_vertically": False,
                     "left": 50, "top": 0, "right": 100, "bottom": 30},
                ],
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

                    def evaluate(self, script, arg=None, **kwargs):
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
            ("associated-label", '<label for="email">Email address</label>'
             '<input id="email" type="email" value="synthetic-private-value">', "", False),
            ("hidden", '<input type="email" aria-label="Email address" style="display:none">', "", True),
            ("disabled", '<input type="email" aria-label="Email address" disabled>', "", True),
            ("readonly", '<input type="email" aria-label="Email address" readonly>', "", True),
            ("clip-path", '<input type="email" aria-label="Email address" style="clip-path:inset(50%)">', "", True),
            ("near-transparent", '<input type="email" aria-label="Email address" style="opacity:0.001">', "", True),
            ("overflow-clipped", '<div class="clip"><input type="email" aria-label="Email address"></div>',
             ".clip{position:absolute;left:20px;top:20px;width:30px;height:30px;overflow:hidden}"
             ".clip input{position:absolute;left:40px;top:0;width:120px;height:30px}", True),
            ("nested-disjoint-overflow-clips", '<div class="outer"><div class="inner">'
             '<input type="email" aria-label="Email address"></div></div>',
             ".outer{position:absolute;left:0;top:20px;width:30px;height:30px;overflow:hidden}"
             ".inner{position:absolute;left:50px;top:0;width:50px;height:30px;overflow:hidden}"
             ".inner input{position:absolute;left:-30px;top:0;width:60px;height:20px}", True),
            ("overlay", '<input type="email" aria-label="Email address">'
             '<div class="overlay"></div>',
             ".overlay{position:fixed;inset:0;background:white;z-index:9999}", True),
            ("pointer-events-none-overlay", '<input type="email" aria-label="Email address">'
             '<div class="overlay"></div>',
             ".overlay{position:fixed;inset:0;background:white;z-index:9999;pointer-events:none}", True),
            ("pointer-none-label-overlay-over-scan-limit",
             '<label class="prompt" for="email">Email address</label>'
             '<input class="field" id="email" type="email" '
             'value="synthetic-private-value"><div class="cover"></div>'
             + "<i></i>" * 5001,
             "form{position:relative;height:100px}.prompt{position:absolute;left:10px;"
             "top:10px;width:300px;height:20px}.field{position:absolute;left:10px;top:40px}"
             ".cover{position:absolute;left:145px;top:10px;width:30px;height:20px;"
             "background:white;z-index:10;pointer-events:none}", True),
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
                page.set_content(
                    '<form><label for="email"><span>Email address</span></label>'
                    '<input id="email" type="email" '
                    'value="synthetic-private-value"></form>'
                )
                approved_label_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    approved_label_projection[0]["rendered_labels"],
                    [{"canonical_label": "email address", "has_text": True,
                      "visible": True}],
                )
                self.assertNotIn(
                    "synthetic-private-value", repr(approved_label_projection),
                )
                page.set_content(
                    '<form><label for="custom">Why do you want to work here? '
                    'synthetic-secret-prompt</label>'
                    '<input id="custom" type="text"></form>'
                )
                unknown_label_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    unknown_label_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": True, "visible": True}],
                )
                self.assertEqual(
                    set(unknown_label_projection[0]["rendered_labels"][0]),
                    {"canonical_label", "has_text", "visible"},
                )
                self.assertNotIn(
                    "Why do you want to work here? synthetic-secret-prompt",
                    repr(unknown_label_projection),
                )
                self.assertNotIn(
                    "synthetic-secret-prompt", repr(unknown_label_projection),
                )
                for name, control, css, expected_review in fixtures:
                    with self.subTest(dom_state=name):
                        page.set_content(
                            f"<style>{css}</style><form>{control}</form>"
                        )
                        analysis = self.adapter.analyze_page(page)
                        if name == "pointer-none-label-overlay-over-scan-limit":
                            self.assertGreater(
                                page.locator("*").count(), 5000,
                            )
                            self.assertEqual(
                                page.evaluate(
                                    """() => {
                                      const target = selector => {
                                        const rect = document.querySelector(selector)
                                          .getBoundingClientRect();
                                        return document.elementFromPoint(
                                          rect.left + rect.width / 2,
                                          rect.top + rect.height / 2
                                        )?.tagName;
                                      };
                                      const label = document.querySelector('label');
                                      const range = document.createRange();
                                      range.selectNodeContents(label);
                                      const textRect = range.getBoundingClientRect();
                                      const coverRect = document.querySelector('.cover')
                                        .getBoundingClientRect();
                                      const textTarget = document.elementFromPoint(
                                        textRect.left + textRect.width / 2,
                                        textRect.top + textRect.height / 2
                                      );
                                      return {
                                        label: target('label'),
                                        input: target('input'),
                                        text: textTarget?.tagName,
                                        textCovered: textRect.left < coverRect.right &&
                                          textRect.right > coverRect.left &&
                                          textRect.top < coverRect.bottom &&
                                          textRect.bottom > coverRect.top,
                                        pointerEvents: getComputedStyle(
                                          document.querySelector('.cover')
                                        ).pointerEvents
                                      };
                                    }"""
                                ),
                                {"label": "LABEL", "input": "INPUT",
                                 "text": "LABEL", "textCovered": False,
                                 "pointerEvents": "none"},
                            )
                            label_projection = page.locator("form").first.evaluate(
                                _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                            )
                            self.assertEqual(
                                label_projection[0]["rendered_labels"],
                                [{"canonical_label": None, "has_text": False,
                                  "visible": False}],
                            )
                            control_state = page.locator("form").locator(
                                "input, select, textarea"
                            ).evaluate_all(_CONTROL_STATE_SCRIPT)
                            self.assertIs(control_state[0]["user_visible"], True)
                            self.assertIs(control_state[0]["hit_test_visible"], False)
                            self.assertIs(control_state[0]["user_editable"], False)
                            self.assertEqual(analysis["status"], "UNKNOWN")
                            self.assertEqual(analysis["fields"], [])
                            self.assertTrue(
                                self.adapter.requires_human_review(analysis)
                            )
                            self.assertNotIn(
                                "synthetic-private-value", repr(analysis),
                            )
                            continue
                        self.assertEqual(analysis["status"], "PASS")
                        if name == "associated-label":
                            self.assertEqual(
                                analysis["fields"][0]["labels"], ["email address"],
                            )
                            self.assertIs(analysis["fields"][0]["user_visible"], True)
                            self.assertIs(analysis["fields"][0]["user_editable"], True)
                            self.assertNotIn("synthetic-private-value", repr(analysis))
                        if name == "pointer-events-none-overlay":
                            self.assertEqual(
                                page.evaluate(
                                    """() => document.elementFromPoint(
                                      (() => {
                                        const rect = document.querySelector('input')
                                          .getBoundingClientRect();
                                        return rect.left + rect.width / 2;
                                      })(),
                                      (() => {
                                        const rect = document.querySelector('input')
                                          .getBoundingClientRect();
                                        return rect.top + rect.height / 2;
                                      })()
                                    )?.tagName"""
                                ),
                                "INPUT",
                            )
                            self.assertIs(analysis["fields"][0]["user_visible"], False)
                            self.assertIs(analysis["fields"][0]["user_editable"], False)
                            self.assertNotIn(
                                "synthetic-private-value", repr(analysis),
                            )
                        self.assertEqual(
                            self.adapter.requires_human_review(analysis), expected_review,
                        )
                for attribute, prompt in (
                    ("aria-label", "Why do you want this job?"),
                    ("placeholder", "Tell us about your background"),
                    ("name", "Why do you want this job?"),
                    ("id", "Tell us about your background"),
                ):
                    with self.subTest(custom_prompt_attribute=attribute):
                        aria_attribute = (
                            f'aria-label="{prompt}"' if attribute == "aria-label"
                            else 'aria-label="Email address"'
                        )
                        prompt_attribute = (
                            "" if attribute == "aria-label"
                            else f'{attribute}="{prompt}"'
                        )
                        page.set_content(
                            f'<form><input type="email" {aria_attribute} '
                            f'{prompt_attribute}></form>'
                        )
                        custom_prompt = self.adapter.analyze_page(page)
                        self.assertEqual(custom_prompt["status"], "PASS")
                        self.assertNotIn(prompt, repr(custom_prompt))
                        self.assertTrue(
                            self.adapter.requires_human_review(custom_prompt)
                        )
                custom_type_text = "Why do you want this job?"
                page.set_content(
                    f'<form><input type="{custom_type_text}" '
                    'aria-label="Email address"></form>'
                )
                custom_type = self.adapter.analyze_page(page)
                self.assertEqual(custom_type["status"], "PASS")
                self.assertNotIn(custom_type_text, repr(custom_type))
                self.assertEqual(custom_type["fields"][0]["input_type"], "other")
                self.assertIs(custom_type["fields"][0]["unrecognized_metadata"], True)
                self.assertTrue(self.adapter.requires_human_review(custom_type))
                page.set_content(
                    '<form><select name="reason"><option>'
                    'Why do you want this job? synthetic-private-option</option>'
                    '</select></form>'
                )
                custom_option = self.adapter.analyze_page(page)
                self.assertEqual(custom_option["status"], "PASS")
                self.assertNotIn("synthetic-private-option", repr(custom_option))
                self.assertEqual(custom_option["fields"][0]["options"], [])
                self.assertTrue(self.adapter.requires_human_review(custom_option))
                page.set_content(
                    '<form><label for="why">Why do you want to work here?</label>'
                    '<input id="why" type="text"></form>'
                )
                unknown_label = self.adapter.analyze_page(page)
                self.assertEqual(unknown_label["status"], "UNKNOWN")
                self.assertEqual(unknown_label["fields"], [])
                self.assertNotIn("Why do you want to work here?", repr(unknown_label))
                page.set_content(
                    '<form><label for="email" style="display:none">Email address</label>'
                    '<input id="email" type="email"></form>'
                )
                page.evaluate(
                    """() => {
                      const label = document.querySelector('label');
                      window.hiddenLabelTextReads = 0;
                      Object.defineProperty(label, 'textContent', {
                        configurable: true,
                        get() {
                          window.hiddenLabelTextReads += 1;
                          return 'Email address';
                        }
                      });
                    }"""
                )
                hidden_label_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    hidden_label_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": False, "visible": False}],
                )
                self.assertEqual(page.evaluate("() => window.hiddenLabelTextReads"), 0)
                hidden_label = self.adapter.analyze_page(page)
                self.assertEqual(hidden_label["status"], "UNKNOWN")
                self.assertTrue(self.adapter.requires_human_review(hidden_label))
                for name, css in (
                    ("transparent color", "color:transparent"),
                    ("transparent text fill", "-webkit-text-fill-color:transparent"),
                ):
                    with self.subTest(label_paint=name):
                        page.set_content(
                            f"<style>label{{{css}}}</style><form>"
                            '<label for="email">Email address</label>'
                            '<input id="email" type="email"></form>'
                        )
                        transparent_label = self.adapter.analyze_page(page)
                        self.assertEqual(transparent_label["status"], "UNKNOWN")
                        self.assertTrue(
                            self.adapter.requires_human_review(transparent_label)
                        )
                page.set_content(
                    '<form><label for="email">'
                    '<span style="color:transparent">Email address</span>'
                    '</label><input id="email" type="email"></form>'
                )
                transparent_descendant = self.adapter.analyze_page(page)
                self.assertEqual(transparent_descendant["status"], "UNKNOWN")
                self.assertTrue(
                    self.adapter.requires_human_review(transparent_descendant)
                )
                page.set_content(
                    '<style>.prompt{display:inline-block;width:120px;height:24px}'
                    '.prompt span{font-size:0}</style><form>'
                    '<label class="prompt" for="email"><span>Email address</span></label>'
                    '<input id="email" type="email"></form>'
                )
                label_box = page.locator("label").evaluate(
                    "label => { const rect = label.getBoundingClientRect(); "
                    "return {width: rect.width, height: rect.height}; }"
                )
                self.assertGreater(label_box["width"], 1)
                self.assertGreater(label_box["height"], 1)
                zero_size_text = self.adapter.analyze_page(page)
                self.assertEqual(zero_size_text["status"], "UNKNOWN")
                self.assertTrue(self.adapter.requires_human_review(zero_size_text))
                page.set_content(
                    '<style>form{position:relative;height:100px}.prompt{position:absolute;'
                    'left:10px;top:10px;width:80px;height:20px}.field{position:absolute;'
                    'left:10px;top:40px}.cover{position:absolute;left:10px;top:10px;'
                    'width:80px;height:20px;background:white;z-index:10}</style>'
                    '<form><label class="prompt" for="email">Email address</label>'
                    '<input class="field" id="email" type="email">'
                    '<div class="cover"></div></form>'
                )
                covered_label_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    covered_label_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": False, "visible": False}],
                )
                covered_label_state = page.locator("form").locator(
                    "input, select, textarea"
                ).evaluate_all(_CONTROL_STATE_SCRIPT)
                self.assertIs(covered_label_state[0]["user_visible"], True)
                self.assertIs(covered_label_state[0]["user_editable"], True)
                self.assertEqual(
                    page.evaluate(
                        """() => {
                          const rect = document.querySelector('label').getBoundingClientRect();
                          const target = document.elementFromPoint(
                            rect.left + rect.width / 2, rect.top + rect.height / 2
                          );
                          return target && target.className;
                        }"""
                    ),
                    "cover",
                )
                covered_label = self.adapter.analyze_page(page)
                self.assertEqual(covered_label["status"], "UNKNOWN")
                self.assertEqual(covered_label["reason"], "NO_VISIBLE_ASSOCIATED_LABEL")
                self.assertEqual(covered_label["fields"], [])
                self.assertTrue(self.adapter.requires_human_review(covered_label))
                page.set_content(
                    '<style>form{position:relative;height:130px}.prompt{position:absolute;'
                    'left:10px;top:10px;display:block;width:240px;height:60px}.field{'
                    'position:absolute;left:10px;top:90px}.cover{position:absolute;left:10px;'
                    'top:10px;width:105px;height:22px;background:white;z-index:10}</style>'
                    '<form><label class="prompt" for="email">Email address</label>'
                    '<input class="field" id="email" type="email">'
                    '<div class="cover"></div></form>'
                )
                label_and_text_targets = page.evaluate(
                    """() => {
                      const label = document.querySelector('label');
                      const labelRect = label.getBoundingClientRect();
                      const range = document.createRange();
                      range.selectNodeContents(label);
                      const textRect = range.getBoundingClientRect();
                      const targetClass = (x, y) => {
                        const target = document.elementFromPoint(x, y);
                        return target && target.className;
                      };
                      return {
                        labelCenter: targetClass(
                          labelRect.left + labelRect.width / 2,
                          labelRect.top + labelRect.height / 2
                        ),
                        textCenter: targetClass(
                          textRect.left + textRect.width / 2,
                          textRect.top + textRect.height / 2
                        )
                      };
                    }"""
                )
                self.assertEqual(label_and_text_targets["labelCenter"], "prompt")
                self.assertEqual(label_and_text_targets["textCenter"], "cover")
                text_covered_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    text_covered_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": False, "visible": False}],
                )
                text_covered = self.adapter.analyze_page(page)
                self.assertEqual(text_covered["status"], "UNKNOWN")
                self.assertEqual(text_covered["reason"], "NO_VISIBLE_ASSOCIATED_LABEL")
                self.assertTrue(self.adapter.requires_human_review(text_covered))
                page.set_content(
                    '<style>form{position:relative;height:130px}.prompt{position:absolute;'
                    'left:10px;top:10px;display:block;width:240px;height:60px}.field{'
                    'position:absolute;left:10px;top:90px}.cover{position:absolute;left:10px;'
                    'top:10px;width:32px;height:22px;background:white;z-index:10}</style>'
                    '<form><label class="prompt" for="email">Email address</label>'
                    '<input class="field" id="email" type="email">'
                    '<div class="cover"></div></form>'
                )
                partial_text_targets = page.evaluate(
                    """() => {
                      const label = document.querySelector('label');
                      const labelRect = label.getBoundingClientRect();
                      const range = document.createRange();
                      range.selectNodeContents(label);
                      const textRect = range.getBoundingClientRect();
                      const targetClass = (x, y) => {
                        const target = document.elementFromPoint(x, y);
                        return target && target.className;
                      };
                      return {
                        labelCenter: targetClass(
                          labelRect.left + labelRect.width / 2,
                          labelRect.top + labelRect.height / 2
                        ),
                        textCenter: targetClass(
                          textRect.left + textRect.width / 2,
                          textRect.top + textRect.height / 2
                        ),
                        coveredGlyph: targetClass(
                          textRect.left + 5, textRect.top + textRect.height / 2
                        )
                      };
                    }"""
                )
                self.assertEqual(partial_text_targets["labelCenter"], "prompt")
                self.assertEqual(partial_text_targets["textCenter"], "prompt")
                self.assertEqual(partial_text_targets["coveredGlyph"], "cover")
                partial_text_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    partial_text_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": False, "visible": False}],
                )
                partial_text_control_state = page.locator("form").locator(
                    "input, select, textarea"
                ).evaluate_all(_CONTROL_STATE_SCRIPT)
                self.assertIs(partial_text_control_state[0]["user_visible"], True)
                self.assertIs(partial_text_control_state[0]["user_editable"], True)
                partially_covered = self.adapter.analyze_page(page)
                self.assertEqual(partially_covered["status"], "UNKNOWN")
                self.assertEqual(
                    partially_covered["reason"], "NO_VISIBLE_ASSOCIATED_LABEL",
                )
                self.assertTrue(self.adapter.requires_human_review(partially_covered))
                page.set_content(
                    '<style>form{position:relative;height:100px}.prompt{position:absolute;'
                    'left:10px;top:10px;width:80px;height:20px}.field{position:absolute;'
                    'left:10px;top:40px}.cover{position:absolute;left:10px;top:10px;'
                    'width:80px;height:20px;background:white;z-index:10;'
                    'pointer-events:none}</style>'
                    '<form><label class="prompt" for="email">Email address</label>'
                    '<input class="field" id="email" type="email" '
                    'value="synthetic-private-value">'
                    '<div class="cover"></div></form>'
                )
                pointer_none_label_projection = page.locator("form").first.evaluate(
                    _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS),
                )
                self.assertEqual(
                    pointer_none_label_projection[0]["rendered_labels"],
                    [{"canonical_label": None, "has_text": False, "visible": False}],
                )
                pointer_none_label_control_state = page.locator("form").locator(
                    "input, select, textarea"
                ).evaluate_all(_CONTROL_STATE_SCRIPT)
                self.assertIs(
                    pointer_none_label_control_state[0]["user_visible"], True,
                )
                self.assertIs(
                    pointer_none_label_control_state[0]["user_editable"], True,
                )
                self.assertEqual(
                    page.evaluate(
                        """() => {
                          const centerTarget = selector => {
                            const rect = document.querySelector(selector).getBoundingClientRect();
                            return document.elementFromPoint(
                              rect.left + rect.width / 2, rect.top + rect.height / 2
                            )?.tagName;
                          };
                          return {
                            label: centerTarget('label'),
                            input: centerTarget('input'),
                            pointerEvents: getComputedStyle(
                              document.querySelector('.cover')
                            ).pointerEvents
                          };
                        }"""
                    ),
                    {"label": "LABEL", "input": "INPUT", "pointerEvents": "none"},
                )
                pointer_none_covered_label = self.adapter.analyze_page(page)
                self.assertEqual(pointer_none_covered_label["status"], "UNKNOWN")
                self.assertEqual(pointer_none_covered_label["fields"], [])
                self.assertTrue(
                    self.adapter.requires_human_review(pointer_none_covered_label)
                )
                self.assertNotIn(
                    "synthetic-private-value", repr(pointer_none_covered_label),
                )
                page.set_content(
                    '<style>form{position:relative;height:100px}.prompt{position:absolute;'
                    'left:10px;top:10px;width:80px;height:20px}.field{position:absolute;'
                    'left:10px;top:40px}.cover{position:absolute;left:10px;top:10px;'
                    'width:80px;height:20px;background:white;z-index:10;'
                    'pointer-events:none}</style>'
                    '<form><label class="prompt" for="email">Email address</label>'
                    '<input class="field" id="email" type="email" '
                    'value="synthetic-private-value">'
                    '<div class="cover"></div></form>'
                )
                page.evaluate(
                    """() => {
                      const nodes = document.createDocumentFragment();
                      for (let index = 0; index < 5001; index += 1) {
                        nodes.appendChild(document.createElement('span'));
                      }
                      document.body.appendChild(nodes);
                    }"""
                )
                large_dom_covered_label = self.adapter.analyze_page(page)
                self.assertEqual(large_dom_covered_label["status"], "UNKNOWN")
                self.assertEqual(large_dom_covered_label["fields"], [])
                self.assertTrue(
                    self.adapter.requires_human_review(large_dom_covered_label)
                )
                self.assertNotIn(
                    "synthetic-private-value", repr(large_dom_covered_label),
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
