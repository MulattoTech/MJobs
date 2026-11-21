"""Remote OK optional salary fields must not crash or invent compensation."""
import unittest

from services.discovery.sources.remoteok import RemoteOK


def raw_job(**changes):
    return {
        "id": 9901,
        "position": "Synthetic Engineer",
        "company": "Synthetic Employer",
        "description": "Build and maintain synthetic systems.",
        **changes,
    }


class RemoteOKSalaryTests(unittest.TestCase):
    def test_invalid_optional_amounts_remain_unknown(self):
        invalid = (
            None, True, False, 0, -1, "not-a-number", "", [], {},
            float("inf"), float("-inf"), float("nan"),
            "Infinity", "-Infinity", "NaN", "1e309", 10 ** 400,
        )
        for field in ("salary_min", "salary_max"):
            for value in invalid:
                with self.subTest(field=field, value=repr(value)[:30]):
                    result = RemoteOK().normalize(raw_job(**{field: value}))
                    self.assertEqual(result["title"], "Synthetic Engineer")
                    self.assertEqual(result["requisition"], "9901")
                    self.assertIsNone(result["attributes"]["salary_min"])
                    self.assertIsNone(result["attributes"]["salary_max"])
                    self.assertEqual(result["attributes"]["salary_source"], "UNKNOWN")

    def test_valid_opposite_bound_is_preserved(self):
        for invalid in ("Infinity", "1e309", True, 10 ** 400):
            for field in ("salary_min", "salary_max"):
                with self.subTest(field=field, value=repr(invalid)[:30]):
                    other = "salary_max" if field == "salary_min" else "salary_min"
                    attributes = RemoteOK().normalize(
                        raw_job(**{field: invalid, other: "120000"}))["attributes"]
                    self.assertIsNone(attributes[field])
                    self.assertEqual(attributes[other], 120000.0)
                    self.assertEqual(attributes["salary_period"], "YEAR")
                    self.assertEqual(attributes["salary_source"], "SOURCE_PROVIDED")

    def test_invalid_amounts_keep_description_fallback_and_raw_evidence(self):
        raw = raw_job(salary_min="Infinity", salary_max=True,
                      description="Salary: $95,000 - $115,000 / year.")
        before = dict(raw)
        attributes = RemoteOK().normalize(raw)["attributes"]
        self.assertEqual(attributes["salary_min"], 95000.0)
        self.assertEqual(attributes["salary_max"], 115000.0)
        self.assertEqual(attributes["salary_period"], "YEAR")
        self.assertEqual(raw, before)

    def test_finite_integer_float_and_numeric_string_amounts_still_work(self):
        for value in (120000, 120000.0, "120000", " 120000 ", "1.2e5"):
            with self.subTest(value=value):
                attributes = RemoteOK().normalize(
                    raw_job(salary_min=value, salary_max=150000))["attributes"]
                self.assertEqual(attributes["salary_min"], 120000.0)
                self.assertEqual(attributes["salary_max"], 150000.0)
                self.assertEqual(attributes["salary_period"], "YEAR")
