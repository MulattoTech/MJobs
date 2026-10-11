"""Deterministic AI work planning limits provider and document candidates."""
import unittest


class AICostControlTests(unittest.TestCase):
    def test_default_funnel_caps_ai_and_reviewed_document_candidates(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        candidates = [
            CandidateScore(f"job-{index:04}", index % 101, document_reviewed=True)
            for index in range(5000)
        ]
        policy = AICostPolicy()

        forward = policy.plan(candidates)
        reverse = policy.plan(reversed(candidates))

        self.assertEqual(forward, reverse)
        self.assertEqual(forward.received_count, 5000)
        self.assertEqual(forward.unique_count, 5000)
        expected = [candidate for candidate in candidates
                    if candidate.deterministic_score >= policy.minimum_score]
        expected.sort(key=lambda candidate: (
            -candidate.deterministic_score, candidate.job_id.casefold(), candidate.job_id,
        ))
        self.assertEqual(forward.ai_candidate_ids,
                         tuple(candidate.job_id for candidate in expected[:75]))
        self.assertLessEqual(len(forward.ai_candidate_ids), 75)
        self.assertEqual(len(forward.document_candidate_ids), 20)
        self.assertTrue(set(forward.document_candidate_ids).issubset(forward.ai_candidate_ids))

    def test_custom_thresholds_filter_before_limits_and_only_reviewed_documents_qualify(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        policy = AICostPolicy(minimum_score=90, max_ai_candidates=2,
                              max_document_candidates=1)
        plan = policy.plan([
            CandidateScore("below", 89, document_reviewed=True),
            CandidateScore("strong-a", 96, document_reviewed=False),
            CandidateScore("strong-b", 96, document_reviewed=True),
            CandidateScore("strong-c", 91, document_reviewed=True),
        ])

        self.assertEqual(plan.ai_candidate_ids, ("strong-a", "strong-b"))
        self.assertEqual(plan.document_candidate_ids, ("strong-b",))
        self.assertEqual(plan.eligible_count, 3)

    def test_identical_duplicates_collapse_and_conflicting_identity_fails_closed(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        policy = AICostPolicy(minimum_score=0, max_ai_candidates=10,
                              max_document_candidates=0)
        duplicate = CandidateScore("same", 88, document_reviewed=False)
        plan = policy.plan([duplicate, duplicate])
        self.assertEqual(plan.received_count, 2)
        self.assertEqual(plan.unique_count, 1)
        self.assertEqual(plan.ai_candidate_ids, ("same",))

        with self.assertRaises(ValueError) as error:
            policy.plan([duplicate, CandidateScore("same", 95, document_reviewed=False)])
        self.assertEqual(str(error.exception), "AI_FUNNEL_DUPLICATE_CONFLICT")

    def test_invalid_policy_and_candidate_data_fail_closed(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        for kwargs in (
            {"minimum_score": 101},
            {"max_ai_candidates": -1},
            {"max_ai_candidates": 1, "max_document_candidates": 2},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AICostPolicy(**kwargs)

        for candidate_factory in (
            lambda: CandidateScore("", 50),
            lambda: CandidateScore("bad-score", 101),
            lambda: CandidateScore("bool-score", True),
            lambda: CandidateScore("bad-review", 50, document_reviewed=1),
        ):
            with self.subTest(candidate_factory=candidate_factory), self.assertRaises(ValueError):
                AICostPolicy().plan([candidate_factory()])

    def test_input_count_is_bounded_even_for_lazy_iterables(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        consumed = []

        def candidates():
            for index in range(6):
                consumed.append(index)
                yield CandidateScore(f"job-{index}", 90)

        policy = AICostPolicy(max_input_candidates=5)
        with self.assertRaises(ValueError) as error:
            policy.plan(candidates())

        self.assertEqual(str(error.exception), "AI_FUNNEL_INPUT_LIMIT")
        self.assertEqual(consumed, [0, 1, 2, 3, 4, 5])

    def test_policy_serialization_and_roundtrip(self):
        from services.ai.cost_controls import AICostPolicy

        policy = AICostPolicy(minimum_score=80, max_input_candidates=3000,
                              max_ai_candidates=50, max_document_candidates=15)
        serialized = policy.to_dict()
        self.assertEqual(serialized, {
            "minimum_score": 80,
            "max_input_candidates": 3000,
            "max_ai_candidates": 50,
            "max_document_candidates": 15,
        })
        reconstructed = AICostPolicy.from_dict(serialized)
        self.assertEqual(reconstructed, policy)

    def test_policy_with_update_validates_constraints(self):
        from services.ai.cost_controls import AICostPolicy

        policy = AICostPolicy()
        updated = policy.with_update(minimum_score=85, max_ai_candidates=50, max_document_candidates=10)
        self.assertEqual(updated.minimum_score, 85)
        self.assertEqual(updated.max_ai_candidates, 50)
        self.assertEqual(updated.max_document_candidates, 10)

        # max_document_candidates cannot exceed max_ai_candidates
        with self.assertRaises(ValueError) as error:
            policy.with_update(max_ai_candidates=10, max_document_candidates=20)
        self.assertEqual(str(error.exception), "AI_FUNNEL_POLICY_INVALID")

    def test_canonical_stages_and_work_plan_serialization(self):
        from services.ai.cost_controls import AICostPolicy, CandidateScore

        policy = AICostPolicy(minimum_score=80, max_ai_candidates=2, max_document_candidates=1)
        plan = policy.plan([
            CandidateScore("job-1", 95, document_reviewed=True),
            CandidateScore("job-2", 85, document_reviewed=False),
            CandidateScore("job-3", 75, document_reviewed=True),
        ])

        stages = policy.canonical_stages(plan)
        self.assertEqual(len(stages), 5)
        self.assertEqual(stages[0]["stage"], "discovered")
        self.assertEqual(stages[0]["count"], 3)
        self.assertEqual(stages[2]["stage"], "scored_eligible")
        self.assertEqual(stages[2]["count"], 2)
        self.assertEqual(stages[3]["stage"], "ai_evaluated")
        self.assertEqual(stages[3]["count"], 2)
        self.assertEqual(stages[4]["stage"], "documents_generated")
        self.assertEqual(stages[4]["count"], 1)

        plan_dict = plan.to_dict()
        self.assertEqual(plan_dict["received_count"], 3)
        self.assertEqual(plan_dict["eligible_count"], 2)
        self.assertEqual(plan_dict["ai_candidate_count"], 2)
        self.assertEqual(plan_dict["document_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
