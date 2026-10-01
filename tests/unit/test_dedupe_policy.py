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

class DedupePolicyTests(unittest.TestCase):
    def test_boilerplate_is_removed_before_similarity(self):
        left = job(description=(
            "Manage Windows infrastructure and Active Directory. "
            "We offer a competitive benefits package. Equal opportunity employer."
        ))
        right = job(description=(
            "Manage Windows infrastructure and Active Directory. "
            "Benefits include medical and dental. EEO employer."
        ))
        result = duplicate_confidence(left, right)
        self.assertEqual(result["reason"], "EXACT_REQUISITION")
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

        no_req_left = job(requisition="", ats_tenant="")
        no_req_right = job(requisition="", ats_tenant="")
        review = duplicate_confidence(
            no_req_left, no_req_right,
            DedupeThresholds(auto_merge_min=0.99, review_min=0.90),
        )
        self.assertEqual(review["confidence"], 0.97)
        self.assertEqual(review["disposition"], "DUPLICATE_REVIEW_REQUIRED")

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
