"""Public posting descriptions retain structure needed by downstream evidence."""
import unittest

from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.remotive import Remotive
from services.discovery.sources.remoteok import RemoteOK
from services.discovery.sources.structured_text import structured_text
from services.discovery.sources.wwr import WeWorkRemotely
from services.documents.cover_letter import _responsibilities


HTML = (
    "&lt;h2&gt;Responsibilities&lt;/h2&gt;"
    "&lt;ul&gt;&lt;li&gt;Maintain Python services.&lt;/li&gt;"
    "&lt;li&gt;Build SQL reports.&lt;/li&gt;&lt;/ul&gt;"
    "&lt;p&gt;Qualifications&lt;/p&gt;"
    "&lt;ul&gt;&lt;li&gt;Maintain current CCNA certification.&lt;/li&gt;&lt;/ul&gt;"
    "&lt;p&gt;Python and SQL experience.&lt;/p&gt;"
    "&lt;script&gt;script-sentinel&lt;/script&gt;"
    "&lt;style&gt;style-sentinel&lt;/style&gt;"
    "&lt;noscript&gt;noscript-sentinel&lt;/noscript&gt;"
)
EXPECTED = (
    "Responsibilities\n- Maintain Python services.\n- Build SQL reports.\n"
    "Qualifications\n- Maintain current CCNA certification.\nPython and SQL experience."
)


class StructuredPostingDescriptionTests(unittest.TestCase):
    def test_structured_text_preserves_boundaries_and_removes_noncontent_elements(self):
        result = structured_text(HTML)

        self.assertEqual(result, EXPECTED)
        for sentinel in ("script-sentinel", "style-sentinel", "noscript-sentinel"):
            self.assertNotIn(sentinel, result)

    def test_structured_text_handles_malformed_lists_breaks_entities_and_unicode(self):
        result = structured_text(
            "<h2>Responsibilities</h2><ul><li>Maintain <strong>Python</strong> services"
            "<li>Build SQL reports</ul><p>Caf&eacute; &amp; APIs<br>Support teams</p>"
            "<!-- comment-sentinel --><script>script-sentinel</script>"
        )

        self.assertEqual(
            result,
            "Responsibilities\n- Maintain Python services\n- Build SQL reports\n"
            "Café & APIs\nSupport teams",
        )
        self.assertNotIn("comment-sentinel", result)
        self.assertNotIn("script-sentinel", result)

    def test_structured_text_omits_hidden_content_from_evidence(self):
        result = structured_text(
            "<p>Visible duty</p><p hidden>Hidden duty</p>"
            "<p aria-hidden='true'>Aria-hidden duty</p>"
            "<div style='display: none'><p>Display-hidden duty</p></div>"
            "<div style='color:red; visibility:hidden !important'>"
            "<p>Visibility-hidden duty</p></div>"
        )

        self.assertEqual(result, "Visible duty")

    def test_responsibility_parser_excludes_action_text_after_qualification_heading(self):
        description = (
            "Responsibilities\n- Maintain Python services.\n"
            "Qualifications\n- Maintain current CCNA certification."
        )

        self.assertEqual(_responsibilities(description), ["Maintain Python services."])

    def test_responsibility_parser_excludes_action_text_after_inline_qualification_heading(self):
        self.assertEqual(
            _responsibilities(
                "Qualifications: Required credentials\n- Maintain current CCNA certification."
            ),
            [],
        )

    def test_responsibility_parser_recognizes_common_qualification_heading_prefixes(self):
        for heading in ("Basic Qualifications", "Minimum Qualifications", "Minimum Requirements"):
            with self.subTest(heading=heading):
                self.assertEqual(
                    _responsibilities(f"{heading}\n- Maintain current CCNA certification."),
                    [],
                )

    def test_responsibility_parser_keeps_action_line_fallback_without_section_headings(self):
        description = "Build Python services.\nMaintain SQL reports."

        self.assertEqual(
            _responsibilities(description),
            ["Build Python services.", "Maintain SQL reports."],
        )

    def test_responsibility_parser_does_not_fallback_from_qualification_only_section(self):
        self.assertEqual(
            _responsibilities("Qualifications\nMaintain current CCNA certification."),
            [],
        )

    def test_responsibility_parser_fallback_survives_section_words_in_duty_prose(self):
        for duty in (
            "Build compliance tools for customer requirements.",
            "Build tools to manage customer responsibilities.",
        ):
            with self.subTest(duty=duty):
                self.assertEqual(_responsibilities(duty), [duty])

    def test_responsibility_section_action_text_can_mention_requirements(self):
        description = (
            "Responsibilities\nBuild compliance tools for customer requirements.\n"
            "Qualifications\nMaintain current CCNA certification."
        )

        self.assertEqual(
            _responsibilities(description),
            ["Build compliance tools for customer requirements."],
        )

    def test_source_adapters_use_structured_text_only_for_descriptions(self):
        adapters_and_payloads = (
            (Greenhouse("example"), {
                "id": 1, "title": "  Senior   Python Engineer  ",
                "location": {"name": "  Remote   USA  "}, "content": HTML,
                "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
            }, "content"),
            (Remotive(), {
                "id": 1, "title": "  Senior   Python Engineer  ",
                "company_name": "  Example   Company  ",
                "candidate_required_location": "  Remote   USA  ",
                "description": HTML,
            }, "description"),
            (RemoteOK(), {
                "id": 1, "position": "  Senior   Python Engineer  ",
                "company": "  Example   Company  ", "location": "  Remote   USA  ",
                "description": HTML,
            }, "description"),
            (WeWorkRemotely(), {
                "title": "Example Company: Senior Python Engineer",
                "guid": "https://weworkremotely.com/remote-jobs/example-1",
                "link": "https://weworkremotely.com/remote-jobs/example-1",
                "description": HTML,
            }, "description"),
        )

        for adapter, raw, description_key in adapters_and_payloads:
            with self.subTest(adapter=type(adapter).__name__):
                original = dict(raw)
                normalized = adapter.normalize(raw)
                self.assertEqual(normalized["description"], EXPECTED)
                self.assertEqual(raw, original)
                self.assertNotIn("script-sentinel", normalized["description"])
                self.assertNotIn("style-sentinel", normalized["description"])
                self.assertNotIn("noscript-sentinel", normalized["description"])
                self.assertIsInstance(raw[description_key], str)

        greenhouse = adapters_and_payloads[0][0].normalize(adapters_and_payloads[0][1])
        remotive = adapters_and_payloads[1][0].normalize(adapters_and_payloads[1][1])
        remoteok = adapters_and_payloads[2][0].normalize(adapters_and_payloads[2][1])
        self.assertEqual(greenhouse["title"], "Senior   Python Engineer")
        self.assertEqual(greenhouse["location"], "Remote   USA")
        self.assertEqual(remotive["title"], "Senior   Python Engineer")
        self.assertEqual(remotive["attributes"]["company_name"], "Example   Company")
        self.assertEqual(remoteok["title"], "Senior   Python Engineer")
        self.assertEqual(remoteok["attributes"]["company_name"], "Example   Company")
        self.assertEqual(remoteok["location"], "Remote   USA")
        wwr = adapters_and_payloads[3][0].normalize(adapters_and_payloads[3][1])
        self.assertEqual(wwr["title"], "Senior Python Engineer")
        self.assertEqual(wwr["attributes"]["company_name"], "Example Company")


if __name__ == "__main__":
    unittest.main()
