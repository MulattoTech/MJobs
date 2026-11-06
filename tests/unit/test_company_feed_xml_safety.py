"""Parser-level no-DTD enforcement for public company RSS and Atom feeds."""
import json
import unittest

from services.company_discovery.endpoints import parse_public_job_feed


def xml_feed(root, declaration="", title="Synthetic &amp; Engineer"):
    link = "https://employer.example/jobs/one"
    if root == "rss":
        content = (
            "<rss><channel><item><title>" + title + "</title>"
            "<link>" + link + "</link></item></channel></rss>"
        )
    else:
        content = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<title>" + title + "</title>"
            '<link rel="alternate" type="text/html" href="' + link + '"/>'
            "</entry></feed>"
        )
    return '<?xml version="1.0"?>' + declaration + content


class CompanyFeedXmlSafetyTests(unittest.TestCase):
    def test_document_types_are_rejected_after_encoding_detection(self):
        for root in ("rss", "feed"):
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                for declaration_kind in ("empty", "internal", "external"):
                    with self.subTest(root=root, encoding=encoding, kind=declaration_kind):
                        title = "Synthetic Engineer"
                        if declaration_kind == "internal":
                            declaration = (
                                '<!DOCTYPE ' + root
                                + ' [<!ENTITY role "Synthetic Engineer">]>'
                            )
                            title = "&role;"
                        elif declaration_kind == "external":
                            declaration = '<!DOCTYPE ' + root + ' SYSTEM "urn:synthetic:no-network">'
                        else:
                            declaration = "<!DOCTYPE " + root + ">"
                        # Transport-decoded text may retain an XML-detectable byte pattern.
                        body = xml_feed(root, declaration, title).encode(encoding).decode("latin-1")
                        with self.assertRaisesRegex(ValueError, "^COMPANY_JOB_FEED_INVALID$"):
                            parse_public_job_feed(body, "RSS")

    def test_ordinary_feeds_and_predefined_entities_remain_supported(self):
        for root in ("rss", "feed"):
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                with self.subTest(root=root, encoding=encoding):
                    body = xml_feed(root).encode(encoding).decode("latin-1")
                    rows = parse_public_job_feed(body, "RSS")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["title"], "Synthetic & Engineer")
                    self.assertEqual(rows[0]["link"], "https://employer.example/jobs/one")

    def test_json_feed_literal_declaration_text_is_not_an_xml_document_type(self):
        item = {
            "title": "Synthetic <!DOCTYPE reference>",
            "url": "https://employer.example/jobs/one",
        }
        body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [item]})
        self.assertEqual(parse_public_job_feed(body, "PUBLIC_JSON"), [item])
