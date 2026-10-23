import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.dedupe.policy import (
    DedupeThresholds,
    duplicate_confidence,
    reconcile_authoritative_value,
    reduce_boilerplate,
)
from services.dedupe.reviews import (
    list_duplicate_reviews,
    record_review_decision,
    save_review_candidate,
)
from services.dedupe.signals import dedupe_title_key


def job(**overrides):
    base = {
        "title": "Systems Administrator",
        "description": "Manage Windows infrastructure and Active Directory.",
        "location": "Dallas, TX",
        "remote_type": "HYBRID",
        "application_url": "https://ats.example/jobs/123",
        "ats_tenant": "acme",
        "requisition": "123",
        "company": {"name": "Acme", "domains": ["acme.example"]},
        "attributes": {"country": "US", "state": "TX", "city": "Dallas"},
    }
    base.update(overrides)
    return base


def weak_identity_job(**overrides):
    """Build a same-company posting pair without strong job identifiers."""
    base = job(application_url="", ats_tenant="", requisition="")
    base.update(overrides)
    return base


class DedupePolicyTests(unittest.TestCase):
    def test_boilerplate_only_descriptions_never_auto_merge(self):
        left = weak_identity_job(description=(
            "We offer a competitive benefits package. Equal opportunity employer."
        ))
        right = weak_identity_job(description=(
            "Benefits include medical and dental. EEO employer."
        ))

        self.assertEqual(reduce_boilerplate(left["description"])["comparison_text"], "")
        self.assertEqual(reduce_boilerplate(right["description"])["comparison_text"], "")
        result = duplicate_confidence(left, right)

        # Without strong identity, boilerplate-only similarity can request review,
        # but must never qualify for automatic merging.
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "SAME_COMPANY_TITLE_EXACT_DESCRIPTION")
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertGreaterEqual(result["boilerplate"]["left_removed"], 1)
        self.assertGreaterEqual(result["boilerplate"]["right_removed"], 1)

    def test_boilerplate_is_removed_before_similarity(self):
        left = weak_identity_job(description=(
            "Manage Windows infrastructure and Active Directory. "
            "We offer a competitive benefits package. Equal opportunity employer."
        ))
        right = weak_identity_job(description=(
            "Manage Windows infrastructure and Active Directory. "
            "Benefits include medical and dental. EEO employer."
        ))
        result = duplicate_confidence(left, right)
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "SAME_COMPANY_TITLE_EXACT_DESCRIPTION")
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertGreaterEqual(result["boilerplate"]["left_removed"], 2)
        self.assertGreaterEqual(result["boilerplate"]["right_removed"], 2)

        stripped = reduce_boilerplate(left["description"])
        self.assertIn("Manage Windows infrastructure", stripped["comparison_text"])
        self.assertNotIn("competitive benefits", stripped["comparison_text"])

    def test_confidence_is_explainable_and_thresholds_are_configurable(self):
        left = job(application_url="https://ats.example/jobs/123?utm_source=board")
        right = job(application_url="https://ats.example/jobs/123")
        result = duplicate_confidence(left, right)
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")
        self.assertIn("ATS_TENANT_REQUISITION", result["signals"]["strong_signals"])

        no_req_left = job(requisition="", ats_tenant="")
        no_req_right = job(requisition="", ats_tenant="")
        review = duplicate_confidence(
            no_req_left, no_req_right,
            DedupeThresholds(auto_merge_min=0.99, review_min=0.90),
        )
        self.assertEqual(review["confidence"], 0.97)
        self.assertEqual(review["disposition"], "DUPLICATE_REVIEW_REQUIRED")

    def test_same_ats_requisition_overrides_changed_route_and_title(self):
        left = job(
            application_url="https://board-a.example/posting/old",
            title="Windows Sysadmin",
        )
        right = job(
            application_url="https://board-b.example/posting/new",
            title="Infrastructure Operations Lead",
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")
        self.assertEqual(result["signals"]["strong_signals"], ["ATS_TENANT_REQUISITION"])

    def test_same_ats_requisition_does_not_override_mismatched_company(self):
        left = job(
            application_url="https://acme.example/jobs/123",
            title="Windows Systems Administrator",
            description="Maintain Windows servers and Active Directory.",
            company={"name": "Acme", "domains": ["acme.example"]},
        )
        right = job(
            application_url="https://other.example/jobs/123",
            title="Senior Payroll Analyst",
            description="Reconcile payroll, tax filings, and employee benefits.",
            company={"name": "Other Company", "domains": ["other.example"]},
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(left["ats_tenant"], right["ats_tenant"])
        self.assertEqual(left["requisition"], right["requisition"])
        self.assertFalse(result["signals"]["company"]["same_company"])
        self.assertFalse(result["signals"]["strong_match"])
        self.assertIn("ATS_TENANT_REQUISITION", result["signals"]["strong_signals"])
        self.assertLessEqual(result["confidence"], 0.64)
        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")

    def test_abbreviation_equivalent_title_without_strong_id_requires_review(self):
        left = weak_identity_job(title="Windows Sysadmin")
        right = weak_identity_job(title="Windows Systems Administrator")

        result = duplicate_confidence(left, right)

        self.assertEqual(dedupe_title_key(left["title"]),
                         dedupe_title_key(right["title"]))
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertTrue(result["signals"]["title"]["equal"])

    def test_genuine_title_key_drift_without_strong_id_is_not_merged(self):
        left = weak_identity_job(title="Windows Systems Administrator")
        right = weak_identity_job(title="Windows Systems Engineer")

        self.assertNotEqual(dedupe_title_key(left["title"]),
                            dedupe_title_key(right["title"]))
        result = duplicate_confidence(left, right)

        self.assertLessEqual(result["confidence"], 0.64)
        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["signals"]["strong_signals"], [])

    def test_same_ats_requisition_with_changed_salary_remains_exact_duplicate(self):
        left = job(
            application_url="https://board-a.example/posting/one",
            attributes={
                "country": "US", "state": "TX", "city": "Dallas",
                "salary_min": 100_000, "salary_max": 120_000,
                "salary_currency": "USD", "salary_period": "YEAR",
            },
        )
        right = job(
            application_url="https://board-b.example/posting/two",
            attributes={
                "country": "US", "state": "TX", "city": "Dallas",
                "salary_min": 130_000, "salary_max": 150_000,
                "salary_currency": "USD", "salary_period": "YEAR",
            },
        )

        result = duplicate_confidence(left, right)

        self.assertNotEqual(left["attributes"]["salary_min"], right["attributes"]["salary_min"])
        self.assertTrue(result["signals"]["company"]["same_company"])
        self.assertEqual(result["signals"]["strong_signals"], ["ATS_TENANT_REQUISITION"])
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["disposition"], "AUTO_MERGE_ELIGIBLE")
        self.assertEqual(result["reason"], "EXACT_REQUISITION")

    def test_salary_only_change_without_strong_id_still_requires_review(self):
        left = weak_identity_job(attributes={
            "country": "US", "state": "TX", "city": "Dallas",
            "salary_min": 100_000, "salary_max": 120_000,
            "salary_currency": "USD", "salary_period": "YEAR",
        })
        right = weak_identity_job(attributes={
            "country": "US", "state": "TX", "city": "Dallas",
            "salary_min": 110_000, "salary_max": 130_000,
            "salary_currency": "USD", "salary_period": "YEAR",
        })

        result = duplicate_confidence(left, right)

        self.assertEqual(left["ats_tenant"], "")
        self.assertEqual(right["ats_tenant"], "")
        self.assertEqual(left["requisition"], "")
        self.assertEqual(right["requisition"], "")
        self.assertEqual(left.get("employer_requisition", ""), "")
        self.assertEqual(right.get("employer_requisition", ""), "")
        self.assertNotEqual(left["attributes"]["salary_min"], right["attributes"]["salary_min"])
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(result["signals"]["strong_signals"], [])

    def test_different_requisition_title_and_description_stays_separate(self):
        left = weak_identity_job(
            title="Windows Systems Administrator",
            description="Maintain Windows servers, Active Directory, and PowerShell automation.",
            requisition="REQ-A",
        )
        right = weak_identity_job(
            title="Senior Payroll Analyst",
            description="Reconcile payroll, tax filings, and employee benefits administration.",
            requisition="REQ-B",
        )

        result = duplicate_confidence(left, right)

        self.assertLessEqual(result["confidence"], 0.64)
        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["signals"]["strong_signals"], [])

    def test_same_ats_tenant_with_distinct_requisitions_stays_separate(self):
        left = job(
            title="Windows Systems Administrator",
            description="Maintain Windows servers, Active Directory, and PowerShell automation.",
            application_url="https://ats.example/jobs/req-a",
            ats_tenant="acme-ats",
            requisition="REQ-A",
        )
        right = job(
            title="Senior Payroll Analyst",
            description="Reconcile payroll, tax filings, and employee benefits administration.",
            application_url="https://ats.example/jobs/req-b",
            ats_tenant="acme-ats",
            requisition="REQ-B",
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(left["company"], right["company"])
        self.assertEqual(left["ats_tenant"], right["ats_tenant"])
        self.assertTrue(left["requisition"] and right["requisition"])
        self.assertNotEqual(left["requisition"], right["requisition"])
        self.assertLessEqual(result["confidence"], 0.64)
        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")
        self.assertNotIn("ATS_TENANT_REQUISITION", result["signals"]["strong_signals"])

    def test_same_requisition_in_different_ats_tenants_stays_separate(self):
        left = job(
            title="Windows Systems Administrator",
            description="Maintain Windows servers, Active Directory, and PowerShell automation.",
            application_url="https://tenant-a.example/jobs/req-123",
            ats_tenant="tenant-a",
            requisition="REQ-123",
        )
        right = job(
            title="Senior Payroll Analyst",
            description="Reconcile payroll, tax filings, and employee benefits administration.",
            application_url="https://tenant-b.example/jobs/req-123",
            ats_tenant="tenant-b",
            requisition="REQ-123",
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(left["company"], right["company"])
        self.assertTrue(left["requisition"] and right["requisition"])
        self.assertEqual(left["requisition"], right["requisition"])
        self.assertNotEqual(left["ats_tenant"], right["ats_tenant"])
        self.assertLessEqual(result["confidence"], 0.64)
        self.assertEqual(result["disposition"], "SEPARATE")
        self.assertEqual(result["reason"], "INSUFFICIENT_EVIDENCE")
        self.assertNotIn("ATS_TENANT_REQUISITION", result["signals"]["strong_signals"])

    def test_same_title_and_description_in_different_city_requires_review(self):
        left = weak_identity_job()
        right = weak_identity_job(
            location="Austin, TX",
            attributes={"country": "US", "state": "TX", "city": "Austin"},
        )

        result = duplicate_confidence(left, right)

        self.assertEqual(left["ats_tenant"], "")
        self.assertEqual(right["ats_tenant"], "")
        self.assertEqual(left["requisition"], "")
        self.assertEqual(right["requisition"], "")
        self.assertEqual(left.get("employer_requisition", ""), "")
        self.assertEqual(right.get("employer_requisition", ""), "")
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["disposition"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(result["signals"]["strong_signals"], [])
        self.assertTrue(result["signals"]["description"]["exact"])
        self.assertNotEqual(left["location"], right["location"])

    def test_first_party_precedence_preserves_conflicting_provenance(self):
        result = reconcile_authoritative_value([
            {"value": "REMOTE", "authority": "AGGREGATOR", "source": "board-a", "observed_at": "2026-10-01T10:00:00Z"},
            {"value": "HYBRID", "authority": "VERIFIED_EMPLOYER", "source": "acme.example", "observed_at": "2026-10-01T09:00:00Z"},
        ])
        self.assertEqual(result["value"], "HYBRID")
        self.assertEqual(result["authority"], "VERIFIED_EMPLOYER")
        self.assertTrue(result["conflict"])
        self.assertEqual(len(result["provenance"]), 2)


class DedupeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "company", "name": "Example", "domain": "example.test"})
            for identifier, req in (("job-a", "A"), ("job-b", "B")):
                upsert(connection, m.jobs, {
                    "id": identifier, "company_id": "company", "requisition": req,
                    "title": "Systems Administrator", "description": "Manage systems",
                    "skills": [], "location": "Dallas, TX", "remote_type": "HYBRID",
                    "attributes": {},
                })
    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_review_decisions_persist_without_destructive_merge(self):
        review = save_review_candidate(
            self.store, "job-b", "job-a", 0.87,
            {"reason": "SAME_COMPANY_TITLE_LOCATION_HIGH_DESCRIPTION"},
        )
        self.assertEqual(review["state"], "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual((review["left_job_id"], review["right_job_id"]), ("job-a", "job-b"))
        approved = record_review_decision(self.store, review["id"], "MERGE")
        self.assertEqual(approved["state"], "MERGE_APPROVED")
        self.assertEqual(self.store.count("canonical_jobs"), 2)

        kept = record_review_decision(self.store, review["id"], "KEEP_SEPARATE")
        self.assertEqual(kept["state"], "KEEP_SEPARATE")
        self.assertEqual(self.store.count("canonical_jobs"), 2)

    def test_never_merge_is_sticky_when_candidate_is_seen_again(self):
        review = save_review_candidate(self.store, "job-a", "job-b", 0.87, {"v": 1})
        record_review_decision(self.store, review["id"], "NEVER_MERGE")
        repeated = save_review_candidate(self.store, "job-a", "job-b", 0.99, {"v": 2})
        self.assertEqual(repeated["state"], "NEVER_MERGE")
        self.assertEqual(repeated["decision"], "NEVER_MERGE")
        self.assertEqual(repeated["evidence"], {"v": 1})

    def test_api_lists_reviews_and_records_user_decision(self):
        review = save_review_candidate(self.store, "job-a", "job-b", 0.80, {"reason": "POSSIBLE"})
        with TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )) as client:
            rows = client.get("/duplicate-reviews").json()["duplicate_reviews"]
            self.assertEqual([row["id"] for row in rows], [review["id"]])
            response = client.put(
                f"/duplicate-reviews/{review['id']}",
                json={"action": "NEVER_MERGE"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"], "NEVER_MERGE")
        self.assertEqual(list_duplicate_reviews(self.store, "NEVER_MERGE")[0]["id"], review["id"])


if __name__ == "__main__":
    unittest.main()
