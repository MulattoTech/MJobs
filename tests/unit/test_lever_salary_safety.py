"""Malformed optional Lever pay must not discard valid employer postings."""
import unittest
from copy import deepcopy

import httpx

from services.discovery.sources.lever import Lever
from tests.unit.test_lever_source import posting

DESCRIPTION = "Salary: $80,000 - $100,000 per year. Remote Python role."


class LeverSalarySafetyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Lever("global:example")

    def row(self, **salary):
        return dict(posting(), description=DESCRIPTION, salaryRange=salary)

    def assert_description_salary(self, row):
        normalized = self.adapter.normalize(row)
        self.assertEqual(normalized["requisition"], row["id"])
        attributes = normalized["attributes"]
        self.assertEqual(attributes["salary_min"], 80000)
        self.assertEqual(attributes["salary_max"], 100000)
        self.assertEqual(attributes["salary_period"], "YEAR")
        self.assertEqual(attributes["salary_currency"], "USD")
        self.assertEqual(attributes["salary_source"], "EMPLOYER_PROVIDED")

    def test_oversized_integer_bounds_preserve_description_fallback_and_raw_input(self):
        for label, value in (("positive", 10 ** 400), ("negative", -(10 ** 400))):
            for endpoint in ("min", "max"):
                with self.subTest(sign=label, endpoint=endpoint):
                    pay = {"min": 25, "max": 45, "interval": "per-hour", "currency": "USD"}
                    pay[endpoint] = value
                    row = self.row(**pay)
                    original = deepcopy(row)
                    self.assert_description_salary(row)
                    self.assertEqual(row, original)

    def test_nonstring_intervals_preserve_amounts_without_claiming_a_period(self):
        for value in ([], {}, ["per-year"], {"value": "per-year"}, None, True, 17, 17.5):
            with self.subTest(interval_type=type(value).__name__):
                row = self.row(min=25, max=45, interval=value, currency="USD")
                original = deepcopy(row)
                attributes = self.adapter.normalize(row)["attributes"]
                self.assertEqual(attributes["salary_min"], 25)
                self.assertEqual(attributes["salary_max"], 45)
                self.assertEqual(attributes["salary_period"], "UNKNOWN")
                self.assertEqual(attributes["salary_currency"], "USD")
                self.assertEqual(attributes["salary_raw"], row["salaryRange"])
                self.assertEqual(row, original)

    def test_finite_bounds_and_documented_periods_remain_supported(self):
        for interval, period in (("per-year", "YEAR"), ("year", "YEAR"),
                                 ("per-hour", "HOUR"), ("hour", "HOUR"), ("weekly", "UNKNOWN")):
            for low, high in ((0, 0), (25.5, 45.75), (999999998, 999999999)):
                with self.subTest(interval=interval, low=low, high=high):
                    row = self.row(min=low, max=high, interval=interval, currency="USD")
                    attributes = self.adapter.normalize(row)["attributes"]
                    self.assertEqual((attributes["salary_min"], attributes["salary_max"]), (low, high))
                    self.assertEqual(attributes["salary_period"], period)
                    self.assertEqual(attributes["salary_raw"], row["salaryRange"])

    def test_invalid_optional_endpoints_keep_description_salary(self):
        invalid = (("string", "50"), ("true", True), ("false", False), ("none", None),
                   ("object", {}), ("list", []), ("nan", float("nan")),
                   ("infinity", float("inf")), ("negative_infinity", float("-inf")),
                   ("negative", -1), ("upper_limit", 10 ** 9))
        for label, value in invalid:
            for endpoint in ("min", "max"):
                with self.subTest(kind=label, endpoint=endpoint):
                    pay = {"min": 25, "max": 45, "interval": "per-hour", "currency": "USD"}
                    pay[endpoint] = value
                    self.assert_description_salary(self.row(**pay))
        self.assert_description_salary(self.row(min=45, max=25, interval="per-hour", currency="USD"))

    def test_inventory_keeps_bad_optional_pay_and_healthy_neighbor(self):
        rows = [
            dict(posting(1), salaryRange={"min": 25, "max": 10 ** 400, "interval": "per-hour"}),
            dict(posting(2), salaryRange={"min": 25, "max": 45, "interval": ["per-hour"]}),
            dict(posting(3), salaryRange={"min": 25, "max": 45, "interval": "per-hour"}),
        ]
        requests = []

        def serve(request):
            requests.append(request)
            return httpx.Response(200, json=rows)

        adapter = Lever("global:example", transport=httpx.MockTransport(serve))
        batch = adapter.search()
        self.assertTrue(batch.complete_inventory)
        self.assertEqual([item.raw for item in batch.items], rows)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.host, "api.lever.co")
        self.assertEqual(adapter.health(), "HEALTHY")
