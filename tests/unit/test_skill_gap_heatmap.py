"""Current-version skill-gap summaries preserve posting and evidence uncertainty."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from services.learning.skill_gap_heatmap import get_skill_gap_heatmap
from tests.integration.test_spine import PROFILE


class SkillGapHeatmapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'heatmap.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))

        self.required_job = capture_manual_job(self.store, {
            "request_id": "heatmap-required",
            "title": "Backend Engineer",
            "company": "Example One",
            "description": (
                "Required Qualifications:\nPython\nAWS\n"
                "Responsibilities:\nBuild and maintain reliable Python services."
            ),
            "reference_url": "https://jobs.example.invalid/required",
            "application_url": "https://apply.example.invalid/required",
            "profile_id": "fixture",
        })["job"]
        self.unknown_job = capture_manual_job(self.store, {
            "request_id": "heatmap-unknown",
            "title": "Platform Engineer",
            "company": "Example Two",
            "description": "Work with Python and Kubernetes.",
            "reference_url": "https://jobs.example.invalid/unknown",
            "profile_id": "fixture",
        })["job"]
        recalculate(self.store, "fixture")

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_separates_unknown_posting_metadata_from_explicit_unapproved_evidence(self):
        result = get_skill_gap_heatmap(self.store, "fixture")
        by_skill = {row["skill"]: row for row in result["skills"]}

        self.assertEqual(result["jobs_examined"], 2)
        self.assertEqual(by_skill["AWS"]["no_approved_evidence_job_count"], 1)
        self.assertEqual(by_skill["AWS"]["unknown_posting_metadata_job_count"], 0)
        self.assertEqual(by_skill["Kubernetes"]["no_approved_evidence_job_count"], 0)
        self.assertEqual(by_skill["Kubernetes"]["unknown_posting_metadata_job_count"], 1)
        self.assertEqual(by_skill["Python"]["approved_evidence_job_count"], 2)

    def test_heatmap_rows_expose_the_exact_scored_jobs_behind_each_skill(self):
        result = get_skill_gap_heatmap(self.store, "fixture")
        by_skill = {row["skill"]: row for row in result["skills"]}

        aws = by_skill["AWS"]["evidence_jobs"]
        self.assertEqual(len(aws), 1)
        self.assertEqual(aws[0]["job_id"], self.required_job["id"])
        self.assertEqual(aws[0]["title"], "Backend Engineer")
        self.assertEqual(aws[0]["company"], "Example One")
        self.assertEqual(aws[0]["posting_strengths"], ["REQUIRED"])
        self.assertEqual(aws[0]["profile_evidence"], ["NO_APPROVED_EVIDENCE"])
        self.assertFalse(aws[0]["user_reported_applied"])
        self.assertFalse(aws[0]["user_reported_interview"])

        kubernetes = by_skill["Kubernetes"]["evidence_jobs"]
        self.assertEqual(len(kubernetes), 1)
        self.assertEqual(kubernetes[0]["job_id"], self.unknown_job["id"])
        self.assertEqual(kubernetes[0]["posting_strengths"], ["UNKNOWN"])
        self.assertEqual(kubernetes[0]["profile_evidence"], ["NO_APPROVED_EVIDENCE"])

    def test_evidence_examples_are_capped_but_ranked_counts_use_all_current_jobs(self):
        profile_version = self.store.profile("fixture")["version"]
        interview_job_id = self.required_job["id"]
        with self.store.engine.begin() as connection:
            for index in range(12):
                job_id = f"heatmap-evidence-limit-{index:02}"
                company_id = f"heatmap-evidence-company-{index:02}"
                connection.execute(m.companies.insert().values(
                    id=company_id, name=f"Evidence Employer {index:02}",
                    domain="example.test",
                ))
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id=company_id,
                    requisition=f"evidence-{index:02}", title=f"Evidence Role {index:02}",
                    description="Synthetic posting", skills=["AWS"],
                    location="Remote", remote_type="REMOTE", attributes={},
                ))
                connection.execute(m.matches.insert().values(
                    id=f"heatmap-evidence-match-{index:02}", job_id=job_id,
                    profile_id="fixture", profile_version=profile_version, score=42.0,
                    missing=["AWS"],
                    components={"requirements": [{
                        "skill": "AWS", "strength": "REQUIRED",
                        "profile_evidence": "NO_APPROVED_EVIDENCE",
                    }]},
                ))
        application_id = prepare(self.store, interview_job_id, "fixture", self.root)
        set_application_pipeline_status(
            self.store, "fixture", application_id, "INTERVIEW", "heatmap-capped-interview"
        )
        by_skill = {
            row["skill"]: row for row in get_skill_gap_heatmap(self.store, "fixture")["skills"]
        }
        aws = by_skill["AWS"]

        self.assertEqual(aws["evidence_job_count"], 13)
        self.assertEqual(len(aws["evidence_jobs"]), 10)
        self.assertEqual(aws["evidence_jobs"][0]["job_id"], interview_job_id)
        self.assertEqual(
            [job["job_id"] for job in aws["evidence_jobs"]],
            [interview_job_id] + [f"heatmap-evidence-limit-{index:02}"
                                  for index in range(9)],
        )
        self.assertEqual(aws["no_evidence_required_job_count"], 13)

    def test_reports_only_user_reported_application_and_interview_relevance(self):
        application_id = prepare(self.store, self.required_job["id"], "fixture", self.root)
        set_application_pipeline_status(
            self.store, "fixture", application_id, "APPLIED", "heatmap-applied"
        )
        result = get_skill_gap_heatmap(self.store, "fixture")
        by_skill = {row["skill"]: row for row in result["skills"]}

        self.assertEqual(by_skill["Python"]["applied_job_count"], 1)
        self.assertEqual(by_skill["Python"]["interview_job_count"], 0)
        self.assertEqual(by_skill["AWS"]["applied_job_count"], 1)
        self.assertEqual(by_skill["Kubernetes"]["applied_job_count"], 0)
        self.assertEqual(by_skill["AWS"]["no_evidence_applied_job_count"], 1)
        self.assertEqual(by_skill["Python"]["no_evidence_applied_job_count"], 0)
        self.assertEqual(by_skill["Kubernetes"]["no_evidence_applied_job_count"], 0)

    def test_uses_only_current_profile_version_and_rejects_stale_version(self):
        original_version = self.store.profile("fixture")["version"]
        changed_profile = deepcopy(PROFILE)
        changed_profile["facts"].append({
            "id": "approved-aws", "kind": "skill", "text": "AWS",
            "source": "user-confirmed test fact", "confidence": 100,
            "verified": True, "user_approved": True,
        })
        self.store.save_profile(changed_profile)
        current_version = self.store.profile("fixture")["version"]

        result = get_skill_gap_heatmap(self.store, "fixture")
        self.assertEqual(result["profile_version"], current_version)
        self.assertEqual(result["jobs_examined"], 0)
        with self.assertRaisesRegex(ValueError, "stale"):
            get_skill_gap_heatmap(self.store, "fixture", original_version)

    def test_profile_matches_and_approval_evidence_do_not_leak_across_profiles(self):
        other_profile = deepcopy(PROFILE)
        other_profile.update(id="other", name="Other Profile", email="other@example.test")
        other_profile["facts"].append({
            "id": "approved-aws", "kind": "skill", "text": "AWS",
            "source": "user-confirmed test fact", "confidence": 100,
            "verified": True, "user_approved": True,
        })
        self.store.save_profile(other_profile)
        recalculate(self.store, "other")

        fixture_rows = {
            row["skill"]: row for row in get_skill_gap_heatmap(
                self.store, "fixture"
            )["skills"]
        }
        other_rows = {
            row["skill"]: row for row in get_skill_gap_heatmap(
                self.store, "other"
            )["skills"]
        }
        self.assertEqual(fixture_rows["AWS"]["no_approved_evidence_job_count"], 1)
        self.assertEqual(other_rows["AWS"]["no_approved_evidence_job_count"], 0)
        self.assertEqual(other_rows["AWS"]["approved_evidence_job_count"], 1)

    def test_interview_counts_are_distinct_from_applied_and_profile_scoped(self):
        application_id = prepare(self.store, self.required_job["id"], "fixture", self.root)
        set_application_pipeline_status(
            self.store, "fixture", application_id, "TECHNICAL_INTERVIEW", "heatmap-interview"
        )
        result = get_skill_gap_heatmap(self.store, "fixture")
        by_skill = {row["skill"]: row for row in result["skills"]}

        self.assertEqual(by_skill["Python"]["interview_job_count"], 1)
        self.assertEqual(by_skill["AWS"]["interview_job_count"], 1)
        self.assertEqual(by_skill["Python"]["applied_job_count"], 0)
        self.assertEqual(by_skill["Kubernetes"]["interview_job_count"], 0)
        self.assertEqual(by_skill["AWS"]["no_evidence_interview_job_count"], 1)
        self.assertEqual(by_skill["Python"]["no_evidence_interview_job_count"], 0)


if __name__ == "__main__":
    unittest.main()
