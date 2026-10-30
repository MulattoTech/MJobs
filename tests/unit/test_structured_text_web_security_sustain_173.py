"""Synthetic XSS regression coverage for third-party posting descriptions."""
from copy import deepcopy
from html import escape
import unittest

from services.discovery.sources.lever import Lever
from services.discovery.sources.structured_text import structured_text


class StructuredTextWebSecurityTests(unittest.TestCase):
    def test_entity_nesting_beyond_decode_limit_fails_closed(self):
        payload = "<script>deep-script-sentinel</script><p>Untrusted text</p>"
        for _ in range(10):
            payload = escape(payload)

        self.assertEqual(structured_text(payload), "")

    def test_nested_encoded_active_content_is_removed(self):
        result = structured_text(
            "&amp;amp;lt;script&amp;amp;gt;script-sentinel"
            "&amp;amp;lt;/script&amp;amp;gt;"
            "&amp;amp;lt;template&amp;amp;gt;template-sentinel"
            "&amp;amp;lt;/template&amp;amp;gt;"
            "&amp;amp;lt;p&amp;amp;gt;Visible role summary"
            "&amp;amp;lt;/p&amp;amp;gt;"
        )

        self.assertEqual(result, "Visible role summary")
        self.assertNotIn("<", result)
        self.assertNotIn("script-sentinel", result)
        self.assertNotIn("template-sentinel", result)

    def test_nested_encoded_event_url_and_style_payloads_are_discarded(self):
        result = structured_text(
            "&amp;amp;lt;img src=x onerror=event-sentinel&amp;amp;gt;"
            "&amp;amp;lt;a href=javascript:javascript-url-sentinel "
            "onclick=click-sentinel&amp;amp;gt;Apply safely"
            "&amp;amp;lt;/a&amp;amp;gt;"
            "&amp;amp;lt;div style=&amp;quot;background:url(javascript:style-sentinel)"
            "&amp;quot;&amp;amp;gt;Remote role&amp;amp;lt;/div&amp;amp;gt;"
        )

        self.assertEqual(result, "Apply safely\nRemote role")
        for sentinel in (
            "event-sentinel",
            "javascript-url-sentinel",
            "click-sentinel",
            "style-sentinel",
        ):
            self.assertNotIn(sentinel, result)
        self.assertNotIn("javascript:", result.casefold())
        self.assertNotIn("style=", result.casefold())

    def test_benign_formatting_links_and_text_remain_usable(self):
        result = structured_text(
            "&amp;amp;lt;h2&amp;amp;gt;Responsibilities&amp;amp;lt;/h2&amp;amp;gt;"
            "&amp;amp;lt;ul&amp;amp;gt;"
            "&amp;amp;lt;li&amp;amp;gt;Build &amp;amp;lt;strong&amp;amp;gt;Python"
            "&amp;amp;lt;/strong&amp;amp;gt; services&amp;amp;lt;/li&amp;amp;gt;"
            "&amp;amp;lt;/ul&amp;amp;gt;"
            "&amp;amp;lt;p&amp;amp;gt;Visit &amp;amp;lt;a "
            "href=&amp;quot;https://jobs.example.invalid/role&amp;quot;&amp;amp;gt;"
            "the role page&amp;amp;lt;/a&amp;amp;gt; &amp;amp;amp;amp; learn more."
            "&amp;amp;lt;/p&amp;amp;gt;"
        )

        self.assertEqual(
            result,
            "Responsibilities\n- Build Python services\nVisit the role page & learn more.",
        )

    def test_lever_normalize_sanitizes_untrusted_description_without_mutating_input(self):
        posting_id = "00000000-0000-4000-8000-000000000173"
        raw = {
            "id": posting_id,
            "text": "Senior Python Engineer",
            "hostedUrl": f"https://jobs.lever.co/example/{posting_id}",
            "categories": {"location": "Remote"},
            "description": (
                "<h2>Responsibilities</h2><ul><li>Build Café services — 東京</li></ul>"
                "<script>lever-script-sentinel</script>"
                "<style>lever-style-sentinel{display:none}</style>"
                "<p hidden>lever-hidden-attribute-sentinel</p>"
                "<p aria-hidden='true'>lever-aria-hidden-sentinel</p>"
                "<div style='display:none'>lever-hidden-style-sentinel</div>"
                "<img src=x onerror='lever-event-sentinel'>"
                "<a href='javascript:lever-url-sentinel' "
                "onclick='lever-click-sentinel'>Apply safely</a>"
                "<div style='background:url(javascript:lever-style-url-sentinel)'>"
                "Visible role note</div>"
            ),
        }
        original = deepcopy(raw)

        normalized = Lever("global:example").normalize(raw)

        self.assertEqual(
            normalized["description"],
            "Responsibilities\n- Build Café services — 東京\nApply safely\nVisible role note",
        )
        for sentinel in (
            "lever-script-sentinel",
            "lever-style-sentinel",
            "lever-hidden-attribute-sentinel",
            "lever-aria-hidden-sentinel",
            "lever-hidden-style-sentinel",
            "lever-event-sentinel",
            "lever-url-sentinel",
            "lever-click-sentinel",
            "lever-style-url-sentinel",
        ):
            self.assertNotIn(sentinel, normalized["description"])
        self.assertNotIn("javascript:", normalized["description"].casefold())
        self.assertNotIn("onerror", normalized["description"].casefold())
        self.assertNotIn("onclick", normalized["description"].casefold())
        self.assertNotIn("style=", normalized["description"].casefold())
        self.assertEqual(raw, original)


if __name__ == "__main__":
    unittest.main()
