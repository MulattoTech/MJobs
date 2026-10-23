"""Synthetic XSS regression coverage for third-party posting descriptions."""
import unittest

from services.discovery.sources.structured_text import structured_text


class StructuredTextWebSecurityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
