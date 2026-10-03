"""Bounded application-link evidence tests (RED before implementation)."""
import json
from types import SimpleNamespace
import unittest

from services.company_discovery.application_links import sample_feed_links
from services.company_discovery.public_client import HTTPStatusError, PublicFetchError


ENDPOINT = "https://employer.example/jobs.xml"


def item(key, link=None, title="Python Engineer"):
    return {"guid": key, "title": title, "link": link or f"https://employer.example/jobs/{key}"}


def page(key="role-1", title="Python Engineer", url=None, *, response_url=None, count=1, headers=None):
    postings = [{"@type": "JobPosting", "identifier": {"value": key}, "title": title,
                 "url": url or f"https://employer.example/jobs/{key}"} for _ in range(count)]
    body = {"@context": "https://schema.org", "@graph": postings}
    return SimpleNamespace(status_code=200, url=response_url or url or f"https://employer.example/jobs/{key}",
                           headers={"content-type": "text/html", **(headers or {})},
                           content=(f'<script type="application/ld+json">{json.dumps(body)}</script>').encode())


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return response


class ApplicationLinkTests(unittest.TestCase):
    def test_second_posting_after_two_million_characters_cannot_be_hidden(self):
        row = item('long')
        response = page('long')
        response.content += b' ' * (2_000_010 - len(response.content)) + page('second').content
        report = sample_feed_links([row], 'RSS', ENDPOINT, Client({row['link']: response}))[0]
        self.assertEqual(report.result, 'UNKNOWN')

    def test_blank_external_identifier_cannot_be_identity_proof(self):
        row = item('   ', link='https://employer.example/jobs/blank')
        response = page('', url=row['link'])
        report = sample_feed_links([row], 'RSS', ENDPOINT, Client({row['link']: response}))[0]
        self.assertEqual(report.result, 'UNKNOWN')
    def test_one_identity_match_and_unknown_are_reported_without_body_storage(self):
        first = item("role-1")
        second = item("role-2")
        client = Client({first["link"]: page("role-1"), second["link"]: page("different")})
        reports = sample_feed_links([first, second], "RSS", ENDPOINT, client, at="2026-10-03T12:00:00Z")
        self.assertEqual(len(reports), 2)
        by_key = {row.external_key: row for row in reports}
        self.assertEqual(by_key["role-1"].result, "ACCURATE")
        self.assertEqual(by_key["role-1"].reason, "JSONLD_IDENTITY_MATCH")
        self.assertEqual(by_key["role-2"].result, "UNKNOWN")
        self.assertNotIn("content", reports[0].__dict__ if hasattr(reports[0], "__dict__") else {})
        self.assertTrue(reports[0].response_sha256)

    def test_ambiguous_missing_and_url_query_mismatch_are_unknown(self):
        ambiguous = item("ambiguous")
        missing = item("missing")
        query = item("query", "https://employer.example/jobs/query?x=1")
        client = Client({ambiguous["link"]: page("ambiguous", count=2),
                         missing["link"]: page("other"),
                         query["link"]: page("query", url="https://employer.example/jobs/query",
                                              response_url=query["link"])})
        reports = sample_feed_links([ambiguous, missing, query], "RSS", ENDPOINT, client, at="2026-10-03T12:00:00Z")
        by_key = {row.external_key: row for row in reports}
        self.assertTrue(all(row.result == "UNKNOWN" for row in reports))
        self.assertEqual(by_key["ambiguous"].reason, "JSONLD_IDENTITY_AMBIGUOUS")
        self.assertEqual(by_key["missing"].reason, "JSONLD_IDENTITY_MISSING")
        self.assertEqual(by_key["query"].reason, "JSONLD_URL_MISMATCH")

    def test_duplicate_identity_conflicting_fingerprints_is_rejected(self):
        first = item("role-1", title="Python Engineer")
        second = item("role-1", title="Different title")
        with self.assertRaisesRegex(ValueError, "SOURCE_DUPLICATE_JOB_ID"):
            sample_feed_links([first, second], "RSS", ENDPOINT, Client({}), at="2026-10-03T12:00:00Z")

    def test_rejected_rows_are_ignored_and_selection_is_daily_deterministic(self):
        rows = [{"guid": "missing-link", "title": "No URL"}, *[item(f"role-{i}") for i in range(6)]]
        responses = {row["link"]: page(row["guid"]) for row in rows[1:]}
        first = sample_feed_links(rows, "RSS", ENDPOINT, Client(responses), at="2026-10-03T12:00:00Z")
        second = sample_feed_links(rows, "RSS", ENDPOINT, Client(responses), at="2026-10-03T12:00:00Z")
        self.assertEqual([row.external_key for row in first], [row.external_key for row in second])
        self.assertLessEqual(len(first), 5)

    def test_daily_rotation_changes_and_reversed_input_keeps_same_sample(self):
        rows = [item(f"role-{i}") for i in range(10)]
        responses = {row["link"]: page(row["guid"]) for row in rows}
        first = sample_feed_links(rows, "RSS", ENDPOINT, Client(responses), at="2026-10-03T12:00:00Z")
        reversed_rows = sample_feed_links(list(reversed(rows)), "RSS", ENDPOINT, Client(responses), at="2026-10-03T12:00:00Z")
        next_day = sample_feed_links(rows, "RSS", ENDPOINT, Client(responses), at="2026-10-04T12:00:00Z")
        self.assertEqual({row.external_key for row in first}, {row.external_key for row in reversed_rows})
        self.assertNotEqual({row.external_key for row in first}, {row.external_key for row in next_day})

    def test_malformed_duplicate_json_and_bounds_never_produce_accuracy(self):
        malformed = item("malformed")
        duplicate = item("duplicate")
        huge = item("huge")
        malformed_page = SimpleNamespace(status_code=200, url=malformed["link"], headers={"content-type": "text/html"},
            content=b'<script type="application/ld+json">{bad</script><script type="application/ld+json">'
                   + json.dumps({"@type": "JobPosting", "identifier": "malformed", "title": malformed["title"], "url": malformed["link"]}).encode()
                   + b"</script>")
        duplicate_page = SimpleNamespace(status_code=200, url=duplicate["link"], headers={"content-type": "text/html"},
            content=b'<script type="application/ld+json">{"@type":"JobPosting","identifier":"duplicate","identifier":"other","title":"Python Engineer","url":"https://employer.example/jobs/duplicate"}</script>')
        huge_page = SimpleNamespace(status_code=200, url=huge["link"], headers={"content-type": "text/html"}, content=b"x" * (2 * 1024 * 1024 + 1))
        reports = sample_feed_links([malformed, duplicate, huge], "RSS", ENDPOINT, Client({
            malformed["link"]: malformed_page, duplicate["link"]: duplicate_page, huge["link"]: huge_page}),
            at="2026-10-03T12:00:00Z")
        by_key = {row.external_key: row for row in reports}
        self.assertTrue(all(row.result == "UNKNOWN" for row in reports))
        self.assertEqual(by_key["huge"].reason, "RESPONSE_TOO_LARGE")

    def test_unsupported_content_type_and_all_unknown_preserve_evidence(self):
        row = item("json")
        response = SimpleNamespace(status_code=200, url=row["link"], headers={"content-type": "application/json"}, content=b"{}")
        report = sample_feed_links([row], "RSS", ENDPOINT, Client({row["link"]: response}), at="2026-10-03T12:00:00Z")[0]
        self.assertEqual(report.result, "UNKNOWN")
        self.assertEqual(report.reason, "CONTENT_TYPE_UNSUPPORTED")
        self.assertTrue(report.identity_evidence)

    def test_budget_stops_remaining_gets_and_missing_response_url_is_unknown(self):
        rows = [item("budget"), item("after")]
        client = Client({rows[0]["link"]: PublicFetchError("REQUEST_BUDGET"), rows[1]["link"]: page("after")})
        reports = sample_feed_links(rows, "RSS", ENDPOINT, client, at="2026-10-03T12:00:00Z")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(row.result == "UNKNOWN" for row in reports))
        missing = item("missing-url")
        response = SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, content=b"")
        report = sample_feed_links([missing], "RSS", ENDPOINT, Client({missing["link"]: response}), at="2026-10-03T12:00:00Z")[0]
        self.assertEqual(report.reason, "RESPONSE_URL_MISSING")

    def test_http_statuses_and_blocked_fetch_have_distinct_unknown_reasons(self):
        not_found = item("404")
        gone = item("410")
        blocked = item("blocked")
        client = Client({not_found["link"]: HTTPStatusError(404), gone["link"]: HTTPStatusError(410),
                         blocked["link"]: PublicFetchError("ROBOTS_DENIED")})
        reports = sample_feed_links([not_found, gone, blocked], "RSS", ENDPOINT, client, at="2026-10-03T12:00:00Z")
        by_key = {row.external_key: row for row in reports}
        self.assertEqual((by_key["404"].result, by_key["404"].reason), ("INACCURATE", "HTTP_404"))
        self.assertEqual((by_key["410"].result, by_key["410"].reason), ("INACCURATE", "HTTP_410"))
        self.assertEqual((by_key["blocked"].result, by_key["blocked"].reason), ("UNKNOWN", "PUBLIC_FETCH_BLOCKED"))

    def test_cancellation_propagates_and_cross_origin_is_denied_before_get(self):
        class Cancelled(Exception):
            pass
        calls = []
        client = Client({})
        with self.assertRaises(Cancelled):
            sample_feed_links([item("one")], "RSS", ENDPOINT, client,
                              check=lambda: (_ for _ in ()).throw(Cancelled()), at="2026-10-03T12:00:00Z")
        cross = item("cross", "https://other.example/jobs/1")
        reports = sample_feed_links([cross], "RSS", ENDPOINT, client, at="2026-10-03T12:00:00Z")
        self.assertEqual(reports[0].reason, "CROSS_ORIGIN")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
