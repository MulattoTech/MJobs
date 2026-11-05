"""Optional Greenhouse pay metadata must not discard a valid posting."""
from copy import deepcopy
import unittest

from services.discovery.sources.greenhouse import Greenhouse


RAW = {
    "id": 1234,
    "title": "Synthetic Python Engineer",
    "content": "Build Python services.",
    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1234",
}
VALID_RANGE = {
    "min_cents": 5_000_000,
    "max_cents": 7_500_000,
    "currency_type": "USD",
}


class GreenhouseSalaryTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Greenhouse("example")

    def test_oversized_integer_bounds_preserve_other_valid_ranges_and_raw_input(self):
        for field in ("min_cents", "max_cents"):
            for value in (10 ** 400, -(10 ** 400)):
                with self.subTest(field=field, positive=value > 0):
                    invalid = {**VALID_RANGE, field: value}
                    raw = {**RAW, "pay_input_ranges": [invalid, dict(VALID_RANGE)]}
                    before = deepcopy(raw)
                    job = self.adapter.normalize(raw)
                    attrs = job["attributes"]
                    self.assertEqual((attrs["salary_min"], attrs["salary_max"]), (50_000, 75_000))
                    self.assertEqual(attrs["salary_currency"], "USD")
                    self.assertEqual(attrs["salary_period"], "UNKNOWN")
                    self.assertEqual(attrs["salary_source"], "EMPLOYER_PROVIDED")
                    self.assertEqual(len(attrs["salary"]), 1)
                    self.assertEqual(job["requisition"], "1234")
                    self.assertEqual(job["application_url"], RAW["absolute_url"])
                    self.assertEqual(raw, before)

    def test_invalid_ranges_retain_description_fallback(self):
        invalid_values = (
            10 ** 400, -(10 ** 400), float("inf"), -float("inf"),
            float("nan"), -1, 100_000_000_000, True, "5000000", None,
        )
        for field in ("min_cents", "max_cents"):
            for index, value in enumerate(invalid_values):
                with self.subTest(field=field, case=index):
                    raw = {
                        **RAW,
                        "content": "Base compensation is $120,000-$150,000 per year.",
                        "pay_input_ranges": [{**VALID_RANGE, field: value}],
                    }
                    attrs = self.adapter.normalize(raw)["attributes"]
                    self.assertIsNone(attrs["salary"])
                    self.assertEqual(
                        (attrs["salary_min"], attrs["salary_max"], attrs["salary_period"]),
                        (120_000, 150_000, "YEAR"),
                    )
                    self.assertEqual(attrs["salary_source"], "EMPLOYER_PROVIDED")

    def test_invalid_ranges_without_fallback_do_not_invent_pay(self):
        for field in ("min_cents", "max_cents"):
            with self.subTest(field=field):
                attrs = self.adapter.normalize({
                    **RAW,
                    "pay_input_ranges": [{**VALID_RANGE, field: 10 ** 400}],
                })["attributes"]
                self.assertIsNone(attrs["salary"])
                self.assertIsNone(attrs["salary_min"])
                self.assertIsNone(attrs["salary_max"])

    def test_supported_ranges_preserve_zero_fractional_cents_and_upper_boundary(self):
        for lower, upper in ((0, 1250.5), (99_999_999_998, 99_999_999_999)):
            with self.subTest(lower=lower, upper=upper):
                attrs = self.adapter.normalize({
                    **RAW,
                    "pay_input_ranges": [{
                        "min_cents": lower, "max_cents": upper, "currency_type": "USD",
                    }],
                })["attributes"]
                self.assertEqual((attrs["salary_min"], attrs["salary_max"]),
                                 (lower / 100, upper / 100))
                self.assertEqual(attrs["salary_currency"], "USD")
                self.assertEqual(attrs["salary_period"], "UNKNOWN")

    def test_reversed_ranges_remain_invalid(self):
        attrs = self.adapter.normalize({
            **RAW, "pay_input_ranges": [{
                "min_cents": 200, "max_cents": 100, "currency_type": "USD",
            }],
        })["attributes"]
        self.assertIsNone(attrs["salary"])
        self.assertIsNone(attrs["salary_min"])
        self.assertIsNone(attrs["salary_max"])


if __name__ == "__main__":
    unittest.main()
