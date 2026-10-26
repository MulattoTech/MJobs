import unittest

from services.dedupe.policy import duplicate_confidence


TENANT = "acme"


def job(**overrides):
    requisition = overrides.pop("requisition", "REQ-100")
    base = {
        "title": "Windows Systems Administrator",
        "description": "Maintain Windows servers, Active Directory, and PowerShell automation.",
        "location": "Dallas, TX",
        "remote_type": "HYBRID",
        "application_url": f"https://boards.example.test/acme/jobs/{requisition}",
        "ats_tenant": TENANT,
        "requisition": requisition,
        "company": {"name": "Acme", "domains": ["acme.example"]},
        "attributes": {
            "country": "US",
            "state": "TX",
            "city": "Dallas",
            "salary_min": 100_000,
            "salary_max": 120_000,
            "salary_currency": "USD",
            "salary_period": "YEAR",
        },
    }
    base.update(overrides)
    return base


def without_strong_identity(**overrides):
    return job(application_url="", ats_tenant="", requisition="", **overrides)


class DedupeSustain216Tests(unittest.TestCase):
    def test_exact_duplicate_is_auto_merge_eligible(self):
        left = job()
        right = job()

        result = duplicate_confidence(left, right)

        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")

    def test_same_ats_requisition_is_merge_eligible_despite_route_and_title_drift(self):
        left = job(
            title="Windows Sysadmin",
            application_url="https://board-a.example.test/jobs/old-route",
        )
        right = job(
            title="Infrastructure Operations Lead",
            application_url="https://board-b.example.test/jobs/new-route",
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(result["signals"]["strong_signals"], ["ATS_TENANT_REQUISITION"])
        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")

    def test_slight_title_difference_without_strong_identity_requires_review(self):
        left = without_strong_identity(title="Windows Sysadmin")
        right = without_strong_identity(title="Windows Systems Administrator")

        result = duplicate_confidence(left, right)

        self.assertTrue(result["signals"]["title"]["equal"])
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")

    def test_changed_salary_does_not_split_same_ats_requisition(self):
        left = job()
        right = job(attributes={
            "country": "US",
            "state": "TX",
            "city": "Dallas",
            "salary_min": 135_000,
            "salary_max": 155_000,
            "salary_currency": "USD",
            "salary_period": "YEAR",
        })

        result = duplicate_confidence(left, right)

        self.assertNotEqual(left["attributes"]["salary_min"], right["attributes"]["salary_min"])
        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")

    def test_different_jobs_at_same_company_remain_distinct(self):
        left = job(requisition="REQ-100")
        right = job(
            requisition="REQ-200",
            title="Senior Payroll Analyst",
            description="Reconcile payroll, tax filings, and employee benefits administration.",
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")

    def test_different_city_is_the_causal_boundary_for_same_title_candidates(self):
        left = without_strong_identity(
            description="Maintain Windows servers and Active Directory for enterprise users.",
        )
        same_city = without_strong_identity(
            description="Maintain Windows servers and Active Directory for enterprise users. Daily.",
        )
        different_city = without_strong_identity(
            description=same_city["description"],
            location="Austin, TX",
            attributes={
                "country": "US",
                "state": "TX",
                "city": "Austin",
                "salary_min": 100_000,
                "salary_max": 120_000,
                "salary_currency": "USD",
                "salary_period": "YEAR",
            },
        )

        same_city_result = duplicate_confidence(left, same_city)
        different_city_result = duplicate_confidence(left, different_city)

        self.assertEqual(
            {key: value for key, value in same_city.items() if key not in {"location", "attributes"}},
            {key: value for key, value in different_city.items() if key not in {"location", "attributes"}},
        )
        self.assertEqual(same_city["attributes"], {**different_city["attributes"], "city": "Dallas"})
        self.assertEqual(same_city_result["reason"], "SAME_COMPANY_TITLE_LOCATION_HIGH_DESCRIPTION")
        self.assertEqual(same_city_result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(different_city_result["reason"], "POSSIBLE_DUPLICATE")
        self.assertEqual(different_city_result["disposition"], "DUPLICATE_REVIEW_REQUIRED")

    def test_boilerplate_heavy_descriptions_cannot_create_an_automatic_merge(self):
        left = without_strong_identity(
            description="We offer a competitive benefits package. Equal opportunity employer.",
        )
        right = without_strong_identity(
            description="Benefits include medical and dental. EEO employer.",
        )

        result = duplicate_confidence(left, right)

        self.assertGreaterEqual(result["boilerplate"]["left_removed"], 1)
        self.assertGreaterEqual(result["boilerplate"]["right_removed"], 1)
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertNotEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
