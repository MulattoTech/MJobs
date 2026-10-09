"""Pure comparison of bounded, already-scored job snapshots."""
from copy import deepcopy
from math import inf
import unittest

from services.matching.job_comparison import (
    JobComparisonInputError,
    compare_scored_jobs,
)


def _requirement(term, source, evidence_ref=None):
    provenance = {"source": source}
    if evidence_ref is not None:
        provenance["evidence_ref"] = evidence_ref
    return {"normalized_term": term, "provenance": provenance}


def _snapshot(job_id, requirements=None, *, score=80.0, contributions=None):
    result = {
        "job_id": job_id,
        "title": f"Role for {job_id}",
        "company": f"Company {job_id}",
        "match_score": score,
        "scoring_contributions": contributions or [],
        # Free text and candidate skills must not become comparison evidence.
        "description": "Python Kubernetes AWS",
        "skills": ["Python", "Kubernetes", "AWS"],
    }
    if requirements is not None:
        result["required_requirements"] = requirements
    return result


class JobComparisonTests(unittest.TestCase):
    def test_preserves_scores_and_contribution_provenance_without_mutation(self):
        contribution = {
            "dimension": "skills",
            "weight": 100.0,
            "raw_score": 71.5,
            "weighted_contribution": 71.5,
            "explanation": "Stored match contribution.",
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }
        snapshots = [
            _snapshot("job-a", [_requirement("python", "normalized_feed")],
                      score=71.5, contributions=[contribution]),
            _snapshot("job-b", [_requirement("python", "normalized_feed")], score=92),
        ]
        original = deepcopy(snapshots)

        result = compare_scored_jobs(
            profile_id="profile-a",
            search_profile_id="search-a",
            job_ids=["job-a", "job-b"],
            snapshots=snapshots,
        )

        self.assertEqual(snapshots, original)
        self.assertEqual(result["jobs"][0]["match_score"], 71.5)
        self.assertEqual(result["jobs"][0]["scoring_contributions"], [contribution])
        self.assertEqual(result["jobs"][1]["match_score"], 92)
        self.assertEqual(result["profile_id"], "profile-a")
        self.assertEqual(result["search_profile_id"], "search-a")

    def test_groups_only_explicit_normalized_requirements_and_deduplicates_rows(self):
        snapshots = [
            _snapshot("job-b", [
                _requirement("python", "source-b", "req-b-python"),
                _requirement("python", "source-b", "req-b-python"),
                _requirement("kubernetes", "source-b", "req-b-k8s"),
            ]),
            _snapshot("job-a", [
                _requirement("kubernetes", "source-a", "req-a-k8s"),
                _requirement("aws", "source-a", "req-a-aws"),
            ]),
            _snapshot("job-c", [
                _requirement("kubernetes", "source-c", "req-c-k8s"),
                _requirement("sql", "source-c", "req-c-sql"),
            ]),
        ]

        result = compare_scored_jobs(
            profile_id="profile-a",
            search_profile_id="search-a",
            job_ids=["job-b", "job-a", "job-c"],
            snapshots=snapshots,
        )

        self.assertEqual(result["shared_requirements"], [{
            "term": "kubernetes",
            "job_ids": ["job-b", "job-a", "job-c"],
            "provenance": [
                {"job_id": "job-b", "source": "source-b", "evidence_ref": "req-b-k8s"},
                {"job_id": "job-a", "source": "source-a", "evidence_ref": "req-a-k8s"},
                {"job_id": "job-c", "source": "source-c", "evidence_ref": "req-c-k8s"},
            ],
        }])
        self.assertEqual(result["job_specific_requirements"], [
            {"term": "aws", "job_id": "job-a",
             "provenance": {"source": "source-a", "evidence_ref": "req-a-aws"}},
            {"term": "python", "job_id": "job-b",
             "provenance": {"source": "source-b", "evidence_ref": "req-b-python"}},
            {"term": "sql", "job_id": "job-c",
             "provenance": {"source": "source-c", "evidence_ref": "req-c-sql"}},
        ])
        self.assertEqual(result["unknowns"], [])

    def test_missing_or_malformed_requirement_evidence_stays_unknown(self):
        snapshots = [
            _snapshot("job-a", [_requirement("python", "source-a")]),
            _snapshot("job-b"),
            _snapshot("job-c", [
                {"normalized_term": "secret raw posting text", "provenance": {"source": ""}},
                {"normalized_term": "Uppercase", "provenance": {"source": "source-c"}},
            ]),
        ]

        result = compare_scored_jobs(
            profile_id="profile-a",
            search_profile_id="search-a",
            job_ids=["job-a", "job-b", "job-c"],
            snapshots=snapshots,
        )

        self.assertEqual(result["shared_requirements"], [])
        self.assertEqual(result["job_specific_requirements"], [
            {"term": "python", "job_id": "job-a",
             "provenance": {"source": "source-a"}},
        ])
        self.assertEqual(result["unknowns"], [
            {"category": "required_terms", "job_ids": ["job-b"],
             "reason": "REQUIREMENTS_MISSING"},
            {"category": "required_terms", "job_ids": ["job-c"],
             "reason": "REQUIREMENT_PROVENANCE_UNKNOWN"},
            {"category": "required_terms", "job_ids": ["job-c"],
             "reason": "REQUIREMENT_TERM_UNKNOWN"},
        ])
        self.assertNotIn("secret raw posting text", repr(result))

    def test_missing_scores_and_contributions_have_explicit_unknown_categories(self):
        missing = _snapshot("job-a", [])
        missing.pop("match_score")
        missing.pop("scoring_contributions")
        snapshots = [missing, _snapshot("job-b", [], score=90, contributions=[])]

        result = compare_scored_jobs(
            profile_id="profile-a",
            search_profile_id="search-a",
            job_ids=["job-a", "job-b"],
            snapshots=snapshots,
        )

        self.assertIsNone(result["jobs"][0]["match_score"])
        self.assertEqual(result["jobs"][0]["scoring_contributions"], [])
        self.assertEqual(result["unknowns"], [
            {"category": "match_score", "job_ids": ["job-a"],
             "reason": "MATCH_SCORE_MISSING"},
            {"category": "contributions", "job_ids": ["job-a"],
             "reason": "SCORING_CONTRIBUTIONS_MISSING"},
        ])

    def test_invalid_scores_are_unknown_instead_of_recalculated(self):
        snapshots = [
            _snapshot("job-a", [], score=10**500, contributions=[]),
            _snapshot("job-b", [], score=inf, contributions=[]),
        ]

        result = compare_scored_jobs(
            profile_id="profile-a",
            search_profile_id="search-a",
            job_ids=["job-a", "job-b"],
            snapshots=snapshots,
        )

        self.assertEqual([job["match_score"] for job in result["jobs"]], [None, None])
        self.assertEqual(result["unknowns"], [{
            "category": "match_score",
            "job_ids": ["job-a", "job-b"],
            "reason": "MATCH_SCORE_INVALID",
        }])

    def test_selected_job_bounds_and_duplicates_are_rejected(self):
        with self.assertRaises(JobComparisonInputError) as too_few:
            compare_scored_jobs(
                profile_id="profile-a", search_profile_id="search-a",
                job_ids=["job-a"], snapshots=[_snapshot("job-a")],
            )
        self.assertEqual(too_few.exception.code, "JOB_COUNT_OUT_OF_RANGE")

        ids = [f"job-{index}" for index in range(11)]
        with self.assertRaises(JobComparisonInputError) as too_many:
            compare_scored_jobs(
                profile_id="profile-a", search_profile_id="search-a",
                job_ids=ids, snapshots=[_snapshot(job_id, []) for job_id in ids],
            )
        self.assertEqual(too_many.exception.code, "JOB_COUNT_OUT_OF_RANGE")

        with self.assertRaises(JobComparisonInputError) as duplicate:
            compare_scored_jobs(
                profile_id="profile-a", search_profile_id="search-a",
                job_ids=["job-a", "job-a"], snapshots=[_snapshot("job-a")],
            )
        self.assertEqual(duplicate.exception.code, "DUPLICATE_JOB_ID")

    def test_ordering_is_stable_when_snapshot_and_requirement_ties_change_order(self):
        first = [
            _snapshot("job-a", [
                _requirement("go", "source-a"),
                _requirement("java", "source-a"),
            ]),
            _snapshot("job-b", [
                _requirement("java", "source-b"),
                _requirement("go", "source-b"),
            ]),
        ]
        second = [
            _snapshot("job-b", list(reversed(first[1]["required_requirements"]))),
            _snapshot("job-a", list(reversed(first[0]["required_requirements"]))),
        ]
        arguments = {
            "profile_id": "profile-a",
            "search_profile_id": "search-a",
            "job_ids": ["job-a", "job-b"],
        }

        result_one = compare_scored_jobs(**arguments, snapshots=first)
        result_two = compare_scored_jobs(**arguments, snapshots=second)

        self.assertEqual(result_one, result_two)
        self.assertEqual(
            [row["term"] for row in result_one["shared_requirements"]],
            ["go", "java"],
        )


if __name__ == "__main__":
    unittest.main()
