"""Duplicate persisted rows retain the pairing of posting strength and evidence."""
from __future__ import annotations

from copy import deepcopy
import unittest

from services.learning.skill_gap_heatmap import _aggregate


def _requirement(strength, evidence, skill="AWS"):
    return {"skill": skill, "strength": strength, "profile_evidence": evidence}


def _match(job_id, requirements):
    return {"job_id": job_id, "components": {"requirements": requirements}}


class SkillGapHeatmapEvidencePairsTests(unittest.TestCase):
    def test_unknown_evidence_cannot_borrow_another_rows_required_strength(self):
        result = _aggregate([
            _match("paired", [
                _requirement("UNKNOWN", "NO_APPROVED_EVIDENCE"),
                _requirement("REQUIRED", "APPROVED", "Amazon Web Services"),
            ]),
        ], {"paired": {"applied": {"paired"}, "interview": {"paired"}}})

        row = result["skills"][0]
        expected = {
            "job_count": 1,
            "required_job_count": 1,
            "unknown_posting_metadata_job_count": 1,
            "approved_evidence_job_count": 1,
            "applied_job_count": 1,
            "interview_job_count": 1,
            "no_approved_evidence_job_count": 0,
            "no_evidence_required_job_count": 0,
            "no_evidence_applied_job_count": 0,
            "no_evidence_interview_job_count": 0,
        }
        self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertEqual(row["skill_id"], "AWS")
        self.assertEqual(row["evidence_jobs"][0]["posting_strengths"],
                         ["REQUIRED", "UNKNOWN"])
        self.assertEqual(row["evidence_jobs"][0]["profile_evidence"],
                         ["APPROVED", "NO_APPROVED_EVIDENCE"])

    def test_optional_gap_does_not_become_required_from_another_row(self):
        for strength in ("PREFERRED", "NICE_TO_HAVE"):
            for required_evidence in ("APPROVED", "UNKNOWN", None):
                with self.subTest(strength=strength, required_evidence=required_evidence):
                    row = _aggregate([
                        _match("optional", [
                            _requirement("REQUIRED", required_evidence),
                            _requirement(strength, "NO_APPROVED_EVIDENCE"),
                        ]),
                    ], {"optional": {"applied": {"optional"}, "interview": {"optional"}}}
                    )["skills"][0]
                    self.assertEqual(row["required_job_count"], 1)
                    self.assertEqual(row["no_approved_evidence_job_count"], 1)
                    self.assertEqual(row["no_evidence_required_job_count"], 0)
                    self.assertEqual(row["no_evidence_applied_job_count"], 1)
                    self.assertEqual(row["no_evidence_interview_job_count"], 1)

    def test_approved_observation_does_not_erase_a_genuine_required_gap(self):
        row = _aggregate([
            _match("conflicting", [
                _requirement("REQUIRED", "NO_APPROVED_EVIDENCE"),
                _requirement("REQUIRED", "APPROVED", "Amazon Web Services"),
                _requirement("UNKNOWN", "APPROVED"),
            ]),
        ], {})["skills"][0]

        self.assertEqual(row["approved_evidence_job_count"], 1)
        self.assertEqual(row["unknown_posting_metadata_job_count"], 1)
        self.assertEqual(row["no_approved_evidence_job_count"], 1)
        self.assertEqual(row["no_evidence_required_job_count"], 1)

    def test_unknown_or_unsupported_strengths_and_evidence_do_not_create_gaps(self):
        for strength in (None, "", "UNCLASSIFIED", "unknown"):
            with self.subTest(strength=strength):
                row = _aggregate([
                    _match("uncertain", [
                        _requirement(strength, "no_approved_evidence"),
                        _requirement("required", "approved"),
                        _requirement("REQUIRED", "UNSUPPORTED"),
                        _requirement("PREFERRED", None),
                    ]),
                ], {})["skills"][0]
                self.assertEqual(row["unknown_posting_metadata_job_count"], 1)
                self.assertEqual(row["no_approved_evidence_job_count"], 0)
                self.assertEqual(row["no_evidence_required_job_count"], 0)

    def test_aliases_duplicates_and_order_preserve_distinct_job_counts(self):
        matches = [
            _match("mixed", [
                _requirement("UNKNOWN", "NO_APPROVED_EVIDENCE"),
                _requirement("REQUIRED", "APPROVED", "Amazon Web Services"),
                _requirement("REQUIRED", "APPROVED"),
            ]),
            _match("genuine", [
                _requirement("REQUIRED", "NO_APPROVED_EVIDENCE"),
                _requirement("REQUIRED", "NO_APPROVED_EVIDENCE", "Amazon Web Services"),
            ]),
        ]
        result = _aggregate(matches, {})
        reordered = deepcopy(list(reversed(matches)))
        for match in reordered:
            match["components"]["requirements"].reverse()
        self.assertEqual(result, _aggregate(reordered, {}))
        self.assertEqual(len(result["skills"]), 1)
        row = result["skills"][0]
        self.assertEqual(row["job_count"], 2)
        self.assertEqual(row["required_job_count"], 2)
        self.assertEqual(row["approved_evidence_job_count"], 1)
        self.assertEqual(row["no_approved_evidence_job_count"], 1)
        self.assertEqual(row["no_evidence_required_job_count"], 1)

    def test_reported_metadata_only_row_cannot_outrank_a_real_gap(self):
        result = _aggregate([
            _match("metadata", [
                _requirement("UNKNOWN", "NO_APPROVED_EVIDENCE", "AWS"),
                _requirement("REQUIRED", "APPROVED", "Amazon Web Services"),
            ]),
            _match("real-gap", [
                _requirement("PREFERRED", "NO_APPROVED_EVIDENCE", "Python"),
            ]),
        ], {"metadata": {"applied": {"metadata"}, "interview": {"metadata"}}})

        self.assertEqual([row["skill_id"] for row in result["skills"]], ["PYTHON", "AWS"])
        self.assertEqual(result["skills"][1]["interview_job_count"], 1)
        self.assertEqual(result["skills"][1]["no_evidence_interview_job_count"], 0)

    def test_optional_gap_does_not_outrank_a_genuine_required_gap(self):
        result = _aggregate([
            _match("optional", [
                _requirement("REQUIRED", "APPROVED", "AWS"),
                _requirement("PREFERRED", "NO_APPROVED_EVIDENCE", "Amazon Web Services"),
            ]),
            _match("required", [
                _requirement("REQUIRED", "NO_APPROVED_EVIDENCE", "Python"),
            ]),
        ], {})

        self.assertEqual([row["skill_id"] for row in result["skills"]], ["PYTHON", "AWS"])


if __name__ == "__main__":
    unittest.main()

