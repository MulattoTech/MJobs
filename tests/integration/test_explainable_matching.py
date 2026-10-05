"""Deterministic match explanations preserve requirement uncertainty."""
import copy
import tempfile
import unittest
from pathlib import Path

from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


class ExplainableMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'matching.db').as_posix()}")
        self.store.migrate()
        profile = copy.deepcopy(PROFILE)
        profile["facts"].append({
            "id": "approved-ad", "kind": "skill", "text": "Administered Active Directory in enterprise environments",
            "source": "user-confirmed test fact", "confidence": 100,
            "verified": True, "user_approved": True,
        })
        profile["facts"].append({
            "id": "pending-server", "kind": "skill", "text": "Windows Server",
            "source": "imported, not reviewed", "confidence": 100,
            "verified": False, "user_approved": False,
        })
        self.store.save_profile(profile)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_manual_capture_persists_skill_mentions_and_explains_unknown_strength(self):
        description = "Maintain Windows Server and Active Directory."
        captured = capture_manual_job(self.store, {
            "request_id": "matching-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": description,
            "reference_url": "https://jobs.example.invalid/role/1",
            "application_url": "https://apply.example.invalid/role/1",
            "source_details": {"level": "unspecified"},
        })["job"]
        self.assertEqual(captured["description"], description)
        self.assertEqual(captured["skills"], ["Windows Server", "Active Directory"])
        self.assertEqual(captured["attributes"]["skill_evidence"], "description_mentions")

        recalculate(self.store, "fixture")
        match = self.store.job(captured["id"], profile_id="fixture")["match"]
        self.assertEqual(match["score"], 50.0)
        requirements = match["components"]["requirements"]
        self.assertEqual([row["strength"] for row in requirements], ["UNKNOWN", "UNKNOWN"])
        self.assertEqual(requirements[0]["profile_evidence"], "NO_APPROVED_EVIDENCE")
        self.assertEqual(requirements[1]["profile_evidence"], "APPROVED")
        self.assertIn("does not state", requirements[0]["explanation"].lower())
        self.assertEqual(match["missing"], ["Windows Server"])

    def test_explicit_preferred_requirement_has_lower_weight_than_required(self):
        from services.discovery.matching import build_explanation

        job = {
            "skills": ["Windows Server", "Active Directory"],
            "attributes": {
                "skill_evidence": "structured_requirements",
                "required_skills": ["Active Directory"],
                "preferred_skills": ["Windows Server"],
            },
        }
        profile = {"facts": [{
            "kind": "skill", "text": "Active Directory", "source": "approved",
            "confidence": 100, "verified": True, "user_approved": True,
        }]}
        result = build_explanation(job, profile)
        self.assertEqual(result["score"], 66.67)
        by_skill = {row["skill"]: row for row in result["components"]["requirements"]}
        self.assertEqual(by_skill["Active Directory"]["strength"], "REQUIRED")
        self.assertEqual(by_skill["Windows Server"]["strength"], "PREFERRED")
        self.assertLess(by_skill["Windows Server"]["weight"], by_skill["Active Directory"]["weight"])
        preferred_only = build_explanation(job, {"facts": [{
            "kind": "skill", "text": "Windows Server", "source": "approved",
            "confidence": 100, "verified": True, "user_approved": True,
        }]})
        self.assertEqual(preferred_only["score"], 33.33)


if __name__ == "__main__":
    unittest.main()
