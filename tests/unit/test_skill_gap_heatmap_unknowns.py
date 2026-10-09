"""Unknown posting metadata and profile evidence remain separate heatmap signals."""
from __future__ import annotations

import unittest

from services.learning.skill_gap_heatmap import _aggregate


class SkillGapHeatmapUnknownsTests(unittest.TestCase):
    def test_unknown_strength_is_not_counted_as_no_approved_profile_evidence(self):
        result = _aggregate([
            {
                "job_id": "job-unknown",
                "missing": [],
                "components": {"requirements": [
                    {
                        "skill": "Kubernetes",
                        "strength": "UNKNOWN",
                        "profile_evidence": "NO_APPROVED_EVIDENCE",
                    },
                ]},
            },
        ], {})

        self.assertEqual(result["jobs_examined"], 1)
        self.assertEqual(result["skills"], [{
            "skill_id": "KUBERNETES",
            "skill": "Kubernetes",
            "job_count": 1,
            "required_job_count": 0,
            "preferred_job_count": 0,
            "nice_to_have_job_count": 0,
            "unknown_posting_metadata_job_count": 1,
            "no_approved_evidence_job_count": 0,
            "approved_evidence_job_count": 0,
            "applied_job_count": 0,
            "interview_job_count": 0,
            "no_evidence_required_job_count": 0,
            "no_evidence_applied_job_count": 0,
            "no_evidence_interview_job_count": 0,
        }])

    def test_duplicate_requirements_count_distinct_jobs_and_order_is_repeatable(self):
        matches = [
            {
                "job_id": "job-b",
                "components": {"requirements": [
                    {"skill": "Python", "strength": "REQUIRED",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                    {"skill": "Python", "strength": "PREFERRED",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                    {"skill": "AWS", "strength": "REQUIRED",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                ]},
            },
            {
                "job_id": "job-a",
                "components": {"requirements": [
                    {"skill": "Python", "strength": "REQUIRED",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                    {"skill": "SQL", "strength": "REQUIRED",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                    {"skill": "K8s", "strength": "UNKNOWN",
                     "profile_evidence": "NO_APPROVED_EVIDENCE"},
                ]},
            },
        ]

        result = _aggregate(matches, {})
        reversed_result = _aggregate(list(reversed(matches)), {})

        self.assertEqual(result, reversed_result)
        self.assertEqual(
            [row["skill_id"] for row in result["skills"]],
            ["PYTHON", "AWS", "SQL", "KUBERNETES"],
        )
        by_skill = {row["skill_id"]: row for row in result["skills"]}
        self.assertEqual(by_skill["PYTHON"]["no_approved_evidence_job_count"], 2)
        self.assertEqual(by_skill["PYTHON"]["required_job_count"], 2)
        self.assertEqual(by_skill["PYTHON"]["preferred_job_count"], 1)
        self.assertEqual(by_skill["AWS"]["no_approved_evidence_job_count"], 1)
        self.assertEqual(by_skill["SQL"]["no_approved_evidence_job_count"], 1)
        self.assertEqual(by_skill["KUBERNETES"]["unknown_posting_metadata_job_count"], 1)
        self.assertEqual(by_skill["KUBERNETES"]["no_approved_evidence_job_count"], 0)

    def test_user_reported_interview_and_application_gaps_sort_before_unreported_gaps(self):
        matches = [
            {"job_id": "interview-job", "components": {"requirements": [
                {"skill": "AWS", "strength": "REQUIRED",
                 "profile_evidence": "NO_APPROVED_EVIDENCE"},
            ]}},
            {"job_id": "applied-job", "components": {"requirements": [
                {"skill": "Linux", "strength": "REQUIRED",
                 "profile_evidence": "NO_APPROVED_EVIDENCE"},
            ]}},
            {"job_id": "unreported-job", "components": {"requirements": [
                {"skill": "Azure", "strength": "REQUIRED",
                 "profile_evidence": "NO_APPROVED_EVIDENCE"},
            ]}},
        ]
        reported = {
            "interview-job": {"applied": set(), "interview": {"interview-job"}},
            "applied-job": {"applied": {"applied-job"}, "interview": set()},
        }

        result = _aggregate(matches, reported)

        self.assertEqual(
            [row["skill_id"] for row in result["skills"]],
            ["AWS", "LINUX", "AZURE"],
        )
        by_skill = {row["skill_id"]: row for row in result["skills"]}
        self.assertEqual(by_skill["AWS"]["no_evidence_interview_job_count"], 1)
        self.assertEqual(by_skill["LINUX"]["no_evidence_applied_job_count"], 1)
        self.assertEqual(by_skill["AZURE"]["no_evidence_applied_job_count"], 0)


if __name__ == "__main__":
    unittest.main()
