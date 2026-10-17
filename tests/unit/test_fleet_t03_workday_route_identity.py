"""Route identity contract for durable Workday checkpoint binding."""
import unittest

from services.applications.adapters.workday_navigation import (
    classify_step,
    workday_route_digest,
)


class FleetT03WorkdayRouteIdentityTests(unittest.TestCase):
    def test_digest_ignores_ephemeral_query_and_fragment_but_keeps_route_path(self):
        route = "https://wd5.myworkdayjobs.com/acme/job/12345/apply"
        self.assertEqual(
            workday_route_digest(route + "?session=one#step"),
            workday_route_digest(route + "?session=two#review"),
        )
        self.assertNotEqual(
            workday_route_digest(route),
            workday_route_digest("https://wd5.myworkdayjobs.com/acme/job/67890/apply"),
        )
        self.assertNotEqual(
            workday_route_digest(route),
            workday_route_digest("https://wd5.myworkdayjobs.com/other/job/12345/apply"),
        )

    def test_digest_normalizes_origin_but_preserves_case_sensitive_path(self):
        self.assertEqual(
            workday_route_digest("https://WD5.MYWORKDAYJOBS.COM.:443/acme/job/REQ-1/apply"),
            workday_route_digest("https://wd5.myworkdayjobs.com/acme/job/REQ-1/apply"),
        )
        self.assertNotEqual(
            workday_route_digest("https://wd5.myworkdayjobs.com/acme/job/REQ-1/apply"),
            workday_route_digest("https://wd5.myworkdayjobs.com/acme/job/req-1/apply"),
        )
        self.assertIsNone(workday_route_digest("https://example.invalid/acme/job/1"))
        self.assertIsNone(workday_route_digest("http://wd5.myworkdayjobs.com/acme/job/1"))

    def test_page_fingerprint_does_not_collide_on_distinct_numeric_route_segments(self):
        def classify(requisition):
            return classify_step({
                "url": f"https://wd5.myworkdayjobs.com/acme/job/{requisition}/apply?session=x",
                "headings": ["My Experience"],
                "field_labels": ["Resume"],
                "actions": ["Save and Continue"],
            })

        first = classify("12345")
        same_route_ephemeral_change = classify_step({
            "url": "https://wd5.myworkdayjobs.com/acme/job/12345/apply?session=y#review",
            "headings": ["My Experience"],
            "field_labels": ["Resume"],
            "actions": ["Save and Continue"],
        })
        other_route = classify("67890")
        self.assertEqual(first["fingerprint"], same_route_ephemeral_change["fingerprint"])
        self.assertNotEqual(first["fingerprint"], other_route["fingerprint"])


if __name__ == "__main__":
    unittest.main()
