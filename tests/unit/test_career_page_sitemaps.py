import unittest

from services.company_discovery.career_pages import parse_sitemap, sitemap_locations


class CareerPageSitemapTests(unittest.TestCase):
    def test_sitemap_locations_returns_only_advertised_same_origin_urls(self):
        html = """
        <html><head>
          <link rel="alternate sitemap" type="application/xml" href="/sitemap.xml">
          <link rel="sitemap" href="https://jobs.example.test/sitemap-careers.xml">
          <link rel="sitemap" href="https://other.example.test/sitemap.xml">
          <link rel="sitemap" href="javascript:alert(1)">
          <link rel="sitemap" href="https://user:secret@example.test/sitemap.xml">
        </head><body><a href="/sitemap.xml">Sitemap</a></body></html>
        """
        self.assertEqual(
            sitemap_locations("https://example.test/about", html),
            ["https://example.test/sitemap.xml"],
        )

    def test_sitemap_locations_accepts_same_origin_xml_sitemap_link(self):
        html = '<a href="/sitemap-index.xml">XML sitemap</a>'
        self.assertEqual(
            sitemap_locations("https://example.test/", html),
            ["https://example.test/sitemap-index.xml"],
        )

    def test_sitemap_locations_skips_invalid_url_syntax(self):
        html = '<link rel="sitemap" href="http://[invalid/sitemap.xml">'
        self.assertEqual(sitemap_locations("https://example.test/", html), [])

    def test_urlset_extracts_only_same_origin_career_pages(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.test/careers/jobs</loc></url>
          <url><loc>https://example.test/about</loc></url>
          <url><loc>https://other.example.test/careers</loc></url>
          <url><loc>https://user:pw@example.test/jobs</loc></url>
        </urlset>"""
        self.assertEqual(
            parse_sitemap("https://example.test/sitemap.xml", xml),
            {"career_pages": ["https://example.test/careers/jobs"], "child_sitemaps": []},
        )

    def test_sitemap_index_returns_same_origin_children_only(self):
        xml = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.test/careers-sitemap.xml</loc></sitemap>
          <sitemap><loc>https://other.example.test/sitemap.xml</loc></sitemap>
          <sitemap><loc>https://example.test/not-a-sitemap.html</loc></sitemap>
        </sitemapindex>"""
        self.assertEqual(
            parse_sitemap("https://example.test/sitemap.xml", xml),
            {
                "career_pages": [],
                "child_sitemaps": [
                    "https://example.test/careers-sitemap.xml",
                    "https://example.test/not-a-sitemap.html",
                ],
            },
        )

    def test_malformed_unsupported_credentialed_and_non_http_inputs_fail_closed(self):
        expected = {"career_pages": [], "child_sitemaps": []}
        self.assertEqual(parse_sitemap("https://example.test/sitemap.xml", "<urlset>"), expected)
        self.assertEqual(parse_sitemap("https://example.test/sitemap.xml", "<rss/>"), expected)
        self.assertEqual(
            parse_sitemap("https://example.test/sitemap.xml", "<urlset><unsupported/></urlset>"),
            expected,
        )
        self.assertEqual(parse_sitemap("https://user:pw@example.test/sitemap.xml", "<urlset/>"), expected)
        self.assertEqual(parse_sitemap("file:///tmp/sitemap.xml", "<urlset/>"), expected)

    def test_rejects_dtd_and_oversized_xml(self):
        expected = {"career_pages": [], "child_sitemaps": []}
        self.assertEqual(
            parse_sitemap(
                "https://example.test/sitemap.xml",
                '<!DOCTYPE urlset [<!ENTITY x "https://example.test/careers">]>'
                '<urlset><url><loc>&x;</loc></url></urlset>',
            ),
            expected,
        )
        self.assertEqual(
            parse_sitemap("https://example.test/sitemap.xml", " " * (2 * 1024 * 1024 + 1)),
            expected,
        )

    def test_rejects_sitemaps_over_entry_limit(self):
        xml = "<urlset>" + "<url><loc>https://example.test/careers</loc></url>" * 10_001 + "</urlset>"
        self.assertEqual(parse_sitemap("https://example.test/sitemap.xml", xml), {
            "career_pages": [], "child_sitemaps": []
        })

    def test_rejects_dtd_after_xml_encoding_detection(self):
        expected = {"career_pages": [], "child_sitemaps": []}
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            for root, item in (("urlset", "url"), ("sitemapindex", "sitemap")):
                for entity in (False, True):
                    with self.subTest(encoding=encoding, root=root, entity=entity):
                        declaration = f"<!DOCTYPE {root}>"
                        location = "https://example.test/careers"
                        if entity:
                            declaration = (
                                f'<!DOCTYPE {root} [<!ENTITY route "{location}">]>'
                            )
                            location = "&route;"
                        xml = (
                            '<?xml version="1.0"?>' + declaration
                            + f"<{root}><{item}><loc>{location}</loc></{item}></{root}>"
                        )
                        # A transport-decoded string can retain the byte pattern
                        # that Expat recognizes as UTF-16 rather than UTF-8.
                        body = xml.encode(encoding).decode("latin-1")
                        self.assertEqual(
                            parse_sitemap("https://example.test/sitemap.xml", body),
                            expected,
                        )

    def test_unsupported_xml_encodings_fail_closed(self):
        for encoding in ("unknown-encoding", "utf-7"):
            with self.subTest(encoding=encoding):
                xml = f'<?xml version="1.0" encoding="{encoding}"?><urlset/>'
                self.assertEqual(
                    parse_sitemap("https://example.test/sitemap.xml", xml),
                    {"career_pages": [], "child_sitemaps": []},
                )

    def test_accepts_ordinary_sitemaps_after_xml_encoding_detection(self):
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            for root, item, target in (
                ("urlset", "url", "career_pages"),
                ("sitemapindex", "sitemap", "child_sitemaps"),
            ):
                with self.subTest(encoding=encoding, root=root):
                    location = "https://example.test/careers"
                    xml = (
                        '<?xml version="1.0"?>'
                        + f"<{root}><{item}><loc>{location}</loc></{item}></{root}>"
                    )
                    body = xml.encode(encoding).decode("latin-1")
                    expected = {"career_pages": [], "child_sitemaps": []}
                    expected[target] = [location]
                    self.assertEqual(
                        parse_sitemap("https://example.test/sitemap.xml", body), expected,
                    )


if __name__ == "__main__":
    unittest.main()
