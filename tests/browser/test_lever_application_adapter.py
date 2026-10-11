"""Connected local-browser contract for the review-first Lever adapter."""
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from services.applications.adapters.lever import LeverApplicationAdapter


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lever_application_form.html"
FIXTURE_ORIGIN = "http://127.0.0.1:8765"


class LeverApplicationAdapterBrowserTests(unittest.TestCase):
    def test_normalizes_supported_lever_route_and_rejects_untrusted_origins(self):
        adapter = LeverApplicationAdapter()
        normalized = adapter.normalize_route({
            "provider": "lever",
            "url": "https://jobs.lever.co/example/12345678-abcd-1234-1234-123456789abc?source=fixture",
        })

        self.assertEqual(normalized, {
            "provider": "lever",
            "tenant": "example",
            "requisition": "12345678-abcd-1234-1234-123456789abc",
            "url": "https://jobs.lever.co/example/12345678-abcd-1234-1234-123456789abc",
        })
        for route in (
            {"provider": "greenhouse", "url": "https://jobs.lever.co/example/123"},
            {"provider": "lever", "url": "http://jobs.lever.co/example/123"},
            {"provider": "lever", "url": "https://jobs.lever.co.attacker.test/example/123"},
            {"provider": "lever", "url": "https://user@jobs.lever.co/example/123"},
            {"provider": "lever", "url": "https://jobs.lever.co:8443/example/123"},
            {"provider": "lever", "url": "https://jobs.lever.co/example/123/extra"},
        ):
            with self.subTest(route=route):
                self.assertIsNone(adapter.normalize_route(route))

    def test_recognizes_form_reviews_sensitive_fields_and_returns_no_values(self):
        fixture_html = FIXTURE.read_text(encoding="utf-8-sig")
        adapter = LeverApplicationAdapter()
        blocked_requests = []
        local_requests = []

        def local_only(route):
            parsed = urlsplit(route.request.url)
            if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.port == 8765):
                local_requests.append(route.request.url)
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture_html)
                return
            blocked_requests.append(route.request.url)
            route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            context.route("**/*", local_only)
            page = context.new_page()
            page.goto(f"{FIXTURE_ORIGIN}/lever/example/application", wait_until="domcontentloaded")

            analysis = adapter.analyze_page(page)

            self.assertEqual(local_requests, [f"{FIXTURE_ORIGIN}/lever/example/application"])
            self.assertEqual(blocked_requests, [], "External browser egress must stay blocked")
            self.assertEqual(analysis.get("status"), "PASS")
            fields = analysis.get("fields")
            self.assertIsInstance(fields, list)
            self.assertGreaterEqual(len(fields), 4)

            def field_for(identity):
                matches = [field for field in fields if identity in repr(field).casefold()]
                self.assertEqual(len(matches), 1, f"Expected one recognized {identity!r} field")
                return matches[0]

            self.assertEqual(field_for("first_name").get("input_type"), "text")
            sensitive = field_for("gender")
            self.assertEqual(sensitive.get("sensitivity"), "SENSITIVE")
            self.assertTrue(adapter.requires_human_review(analysis))
            capabilities = adapter.capabilities()
            self.assertTrue(capabilities.get("route_normalization"))
            self.assertTrue(capabilities.get("form_metadata_extraction"))
            self.assertFalse(capabilities.get("submission"))
            self.assertFalse(capabilities.get("live_form_interaction"))

            serialized = repr(analysis).casefold()
            for private_marker in (
                "private-decoy-applicant",
                "private-applicant@example.invalid",
                "private-decoy-gender",
            ):
                self.assertNotIn(private_marker, serialized)
            self.assertEqual(page.evaluate("window.fixtureActions"), {
                "clicks": 0, "changes": 0, "submits": 0,
            })
            self.assertEqual(page.locator("input[type=file]").evaluate_all(
                "nodes => nodes.map(node => node.files.length)"), [0])
            self.assertEqual(page.locator("#first-name").input_value(), "PRIVATE-DECOY-APPLICANT")
            context.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
