import json
import unittest
from unittest.mock import patch

from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter


class TeamtailorApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TeamtailorApplicationAdapter()

    def test_declares_only_route_and_value_free_review_support(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_normalizes_teamtailor_job_route_and_discards_tracking(self):
        result = self.adapter.normalize_route({
            "provider": "TEAMTAILOR",
            "url": "https://Acme.teamtailor.com/jobs/senior-engineer?source=fixture",
        })
        self.assertEqual(result, {
            "provider": "teamtailor",
            "tenant": "acme",
            "requisition": "senior-engineer",
            "url": "https://acme.teamtailor.com/jobs/senior-engineer",
        })

    def test_rejects_untrusted_hosts_and_route_shapes(self):
        invalid = (
            None,
            {"provider": "greenhouse", "url": "https://acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "http://acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com.attacker.test/jobs/role"},
            {"provider": "teamtailor", "url": "https://user@acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com:444/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role/extra"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/%2fadmin"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role#apply"},
            {"provider": "teamtailor", "url": "https://teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role/"},
        )
        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_conflicting_route_metadata(self):
        url = "https://acme.teamtailor.com/jobs/senior-engineer"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "teamtailor", "url": url, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "teamtailor", "url": url, "requisition": "other",
        }))

    def test_analyze_page_uses_shared_value_free_form_analyzer(self):
        class Locator:
            def count(self):
                return 0

        class Page:
            def locator(self, selector):
                return Locator()

        page = Page()
        expected = {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []}
        with patch("services.applications.adapters.teamtailor.analyze_form_page",
                   return_value=expected) as analyzer:
            self.assertEqual(self.adapter.analyze_page(page), expected)
        analyzer.assert_called_once_with(page)

    def test_challenge_protected_form_fails_closed_without_returning_markup(self):
        class Locator:
            def __init__(self, count):
                self._count = count

            def count(self):
                return self._count

        class Page:
            def locator(self, selector):
                return Locator(1 if "captcha" in selector else 0)

        result = self.adapter.analyze_page(Page())
        self.assertEqual(result, {
            "status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED", "fields": [],
        })
        self.assertNotIn("PRIVATE_CHALLENGE", json.dumps(result))

    def test_unknown_sensitive_ambiguous_and_empty_forms_require_review(self):
        unsafe = (
            None,
            {"status": "UNKNOWN", "fields": []},
            {"status": "PASS", "fields": []},
            {"status": "PASS", "fields": [{
                "ambiguous": True, "sensitivity": "STANDARD", "name": "email",
            }]},
            {"status": "PASS", "fields": [{
                "ambiguous": False, "sensitivity": "SENSITIVE", "name": "visa_status",
            }]},
            {"status": "PASS", "fields": [{
                "ambiguous": False, "sensitivity": "STANDARD", "name": "custom_question",
            }]},
        )
        for analysis in unsafe:
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))

    def test_known_standard_contact_metadata_can_be_reviewed_without_exposing_values(self):
        analysis = {"status": "PASS", "fields": [{
            "ambiguous": False, "sensitivity": "STANDARD", "tag": "input",
            "input_type": "email", "name": "email", "labels": [],
            "user_visible": True, "user_editable": True,
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))
        report = json.dumps(analysis)
        self.assertNotIn("value", report)
        self.assertNotIn("PRIVATE", report)

    def test_hidden_or_noninteractive_inputs_cannot_bypass_manual_review(self):
        unsafe_controls = (
            {"tag": "input", "input_type": "hidden", "name": "email",
             "labels": ["Email"]},
            {"tag": "input", "input_type": "hidden", "name": "resume",
             "labels": ["Resume"]},
            {"tag": "input", "input_type": "submit", "name": "email",
             "labels": ["Email"]},
            {"tag": "input", "input_type": "button", "name": "email",
             "labels": ["Email"]},
            {"tag": "input", "input_type": "image", "name": "email",
             "labels": ["Email"]},
            {"tag": "input", "input_type": "file", "name": "resume",
             "labels": ["Resume"]},
            {"tag": "input", "name": "email", "labels": ["Email"]},
            {"tag": "textarea", "input_type": "textarea", "name": "email",
             "labels": ["Email"]},
        )
        for field in unsafe_controls:
            with self.subTest(field=field):
                self.assertTrue(self.adapter.requires_human_review({
                    "status": "PASS", "fields": [{
                        "ambiguous": False, "sensitivity": "STANDARD",
                        "user_visible": True, "user_editable": True, **field,
                    }],
                }))

    def test_review_gate_requires_explicit_positive_visibility_and_editability(self):
        known_contact = {
            "ambiguous": False, "sensitivity": "STANDARD", "tag": "input",
            "input_type": "email", "name": "email", "labels": ["Email"],
        }
        cases = (
            known_contact,
            {**known_contact, "user_visible": False, "user_editable": True},
            {**known_contact, "user_visible": True, "user_editable": False},
            {**known_contact, "user_visible": True},
            {**known_contact, "user_editable": True},
        )
        for field in cases:
            with self.subTest(field=field):
                self.assertTrue(self.adapter.requires_human_review({
                    "status": "PASS", "fields": [field],
                }))

    def test_analyze_page_attaches_value_free_control_visibility_evidence(self):
        class Locator:
            def __init__(self, selector):
                self.selector = selector

            def count(self):
                return 0

            def evaluate_all(self, script, **kwargs):
                return [{
                    "user_visible": True,
                    "user_editable": True,
                    "paint_styles": [{"clip_path": "none", "clip": "auto"}],
                    "control_rect": {"left": 10, "top": 10, "right": 20, "bottom": 20},
                    "overflow_clip_regions": [],
                    "clip_geometry_transformed": False,
                }]

        class Page:
            def locator(self, selector):
                return Locator(selector)

        analysis = {"status": "PASS", "reason": None, "fields": [{
            "ambiguous": False, "sensitivity": "STANDARD", "tag": "input",
            "input_type": "email", "name": "email", "labels": ["Email"],
        }]}
        with patch("services.applications.adapters.teamtailor.analyze_form_page",
                   return_value=analysis):
            result = self.adapter.analyze_page(Page())
        self.assertEqual(result["fields"][0]["user_visible"], True)
        self.assertEqual(result["fields"][0]["user_editable"], True)
        self.assertFalse(self.adapter.requires_human_review(result))

    def test_analyze_page_css_hidden_disabled_readonly_and_select_controls_require_review(self):
        unsafe_cases = (
            ({"tag": "input", "input_type": "email", "name": "email",
              "labels": ["Email"]},
             {"user_visible": False, "user_editable": False}),  # CSS-hidden
            ({"tag": "input", "input_type": "email", "name": "email",
              "labels": ["Email"]},
             {"user_visible": True, "user_editable": False}),  # disabled
            ({"tag": "input", "input_type": "email", "name": "email",
              "labels": ["Email"]},
             {"user_visible": True, "user_editable": False}),  # readonly
            ({"tag": "select", "input_type": None, "name": "country",
              "labels": ["Country"]},
             {"user_visible": False, "user_editable": False}),  # hidden select
            ({"tag": "select", "input_type": None, "name": "country",
              "labels": ["Country"]},
             {"user_visible": True, "user_editable": False}),  # disabled select
        )

        for field, state in unsafe_cases:
            with self.subTest(field=field, state=state):
                class Locator:
                    def __init__(self, selector):
                        self.selector = selector

                    def count(self):
                        return 0

                    def evaluate_all(self, script, **kwargs):
                        return [state]

                class Page:
                    def locator(self, selector):
                        return Locator(selector)

                analysis = {"status": "PASS", "reason": None, "fields": [{
                    "ambiguous": False, "sensitivity": "STANDARD", **field,
                }]}
                with patch("services.applications.adapters.teamtailor.analyze_form_page",
                           return_value=analysis):
                    result = self.adapter.analyze_page(Page())
                self.assertTrue(self.adapter.requires_human_review(result))

    def test_analyze_page_uses_paint_and_overflow_clip_geometry(self):
        control_rect = {"left": 20, "top": 20, "right": 40, "bottom": 40}
        clip_region = {
            "left": 0, "top": 0, "right": 30, "bottom": 30,
            "clips_horizontally": True, "clips_vertically": True,
        }
        cases = (
            ("clip-path on control", [{
                "clip_path": "inset(50%)", "clip": "auto",
            }], [], False, True),
            ("clip-path on ancestor", [
                {"clip_path": "none", "clip": "auto"},
                {"clip_path": "inset(50%)", "clip": "auto"},
            ], [], False, True),
            ("clip property on control", [{
                "clip_path": "none", "clip": "rect(0px, 0px, 0px, 0px)",
            }], [], False, True),
            ("overflow-x clips the control", [{
                "clip_path": "none", "clip": "auto",
            }], [{
                **clip_region, "clips_vertically": False,
            }], False, True),
            ("overflow-y clips the control", [{
                "clip_path": "none", "clip": "auto",
            }], [{
                **clip_region, "clips_horizontally": False,
            }], False, True),
            ("control inside overflow clip", [{
                "clip_path": "none", "clip": "auto",
            }], [{
                **clip_region, "right": 50, "bottom": 50,
            }], False, False),
            # A 100px CSS clip box scaled to 50 viewport px cannot be
            # compared directly with this child's viewport x=60..70 bounds.
            ("transformed overflow clip fails closed", [{
                "clip_path": "none", "clip": "auto",
            }], [{
                "left": 0, "top": 0, "right": 100, "bottom": 100,
                "clips_horizontally": True, "clips_vertically": True,
            }], True, True),
        )
        required_visibility_checks = (
            "clip_path: styles[index].clipPath",
            "clip: styles[index].clip",
            "style.overflowX",
            "style.overflowY",
            "paint_styles: paintStyles",
            "control_rect: {",
            "overflow_clip_regions: overflowClipRegions",
            "current.clientWidth",
            "current.clientHeight",
            "clip_geometry_transformed: clipGeometryTransformed",
            "transformStyle.transform",
            "transformStyle.scale",
            "transformStyle.rotate",
            "transformStyle.translate",
            "transformStyle.zoom",
        )

        last_script = None
        for (label, paint_styles, overflow_clip_regions,
             clip_geometry_transformed, expected_review) in cases:
            with self.subTest(clipped_case=label):
                state = {
                    "user_visible": True,
                    "user_editable": True,
                    "paint_styles": paint_styles,
                    "control_rect": control_rect,
                    "overflow_clip_regions": overflow_clip_regions,
                    "clip_geometry_transformed": clip_geometry_transformed,
                }
                if label == "transformed overflow clip fails closed":
                    state["control_rect"] = {
                        "left": 60, "top": 20, "right": 70, "bottom": 30,
                    }

                class Locator:
                    def __init__(self, selector):
                        self.selector = selector
                        self.script = None

                    def count(self):
                        return 0

                    def evaluate_all(self, script, **kwargs):
                        self.script = script
                        return [state]

                class Page:
                    def __init__(self):
                        self.controls = None

                    def locator(self, selector):
                        locator = Locator(selector)
                        if "captcha" not in selector and "turnstile" not in selector:
                            self.controls = locator
                        return locator

                analysis = {"status": "PASS", "reason": None, "fields": [{
                    "ambiguous": False, "sensitivity": "STANDARD", "tag": "input",
                    "input_type": "email", "name": "email", "labels": ["Email"],
                }]}
                page = Page()
                with patch("services.applications.adapters.teamtailor.analyze_form_page",
                           return_value=analysis):
                    result = self.adapter.analyze_page(page)

                self.assertEqual(self.adapter.requires_human_review(result), expected_review)
                self.assertIsNotNone(page.controls.script)
                last_script = page.controls.script

        for guard in required_visibility_checks:
            with self.subTest(guard=guard):
                self.assertIn(guard, last_script)


if __name__ == "__main__":
    unittest.main()
