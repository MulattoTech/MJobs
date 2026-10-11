"""Unit and contract tests for Match Weight Configuration (MJ-REQ-117)."""

import unittest

from services.matching.weight_configuration import (
    CONFIGURABLE_DIMENSIONS,
    PRESETS,
    PRESET_BALANCED_DEFAULT,
    PRESET_REMOTE_COMPENSATION_FOCUS,
    PRESET_SENIORITY_EXPERIENCE_FOCUS,
    PRESET_TECHNICAL_SKILLS_FOCUS,
    WeightModel,
    normalize_weights,
    summarize_weight_configuration,
    to_component_dimension_weights,
    validate_weights,
)


class MatchWeightConfigurationTests(unittest.TestCase):
    def test_canonical_spec_preset_matches_authoritative_values(self):
        # Authoritative MJ-REQ-117:
        # title 20, skills 25, experience 15, location 15, salary 10, education 5, company 5, industry 5
        expected = {
            "title": 20.0,
            "skills": 25.0,
            "experience": 15.0,
            "location": 15.0,
            "salary": 10.0,
            "education": 5.0,
            "company": 5.0,
            "industry": 5.0,
        }
        self.assertEqual(PRESET_BALANCED_DEFAULT, expected)

        model = WeightModel()
        self.assertEqual(model.to_dict(), expected)
        self.assertEqual(model.total_weight, 100.0)

    def test_all_presets_sum_to_one_hundred(self):
        for name, (label, preset) in PRESETS.items():
            total = sum(preset.values())
            self.assertAlmostEqual(total, 100.0, places=2, msg=f"Preset {name} does not sum to 100")
            self.assertEqual(set(preset.keys()), set(CONFIGURABLE_DIMENSIONS))

    def test_normalize_weights_scales_proportions_to_100(self):
        # 10 for each of the 8 dimensions -> 12.5% each
        equal_weights = {dim: 10.0 for dim in CONFIGURABLE_DIMENSIONS}
        normalized = normalize_weights(equal_weights)

        self.assertAlmostEqual(sum(normalized.values()), 100.0, places=2)
        for dim, val in normalized.items():
            self.assertAlmostEqual(val, 12.5, delta=0.1)

    def test_normalize_weights_rejects_zero_sum(self):
        zero_weights = {dim: 0.0 for dim in CONFIGURABLE_DIMENSIONS}
        with self.assertRaises(ValueError) as err:
            normalize_weights(zero_weights)
        self.assertIn("must be greater than zero", str(err.exception))

    def test_validate_weights_rejects_invalid_inputs(self):
        # Negative weight
        with self.assertRaises(ValueError):
            validate_weights({"title": -5.0})

        # Non-numeric
        with self.assertRaises(ValueError):
            validate_weights({"title": "heavy"})

        # Boolean (bool is subclass of int in Python)
        with self.assertRaises(ValueError):
            validate_weights({"title": True})

        # Infinity
        with self.assertRaises(ValueError):
            validate_weights({"title": float("inf")})

    def test_to_component_dimension_weights_mapping(self):
        model = WeightModel(
            title=20.0,
            skills=30.0,
            experience=15.0,
            location=15.0,
            salary=10.0,
            education=5.0,
            company=2.5,
            industry=2.5,
        )
        comp_weights = to_component_dimension_weights(model)

        self.assertEqual(comp_weights["title_similarity"], 20.0)
        # Skills 30 split 65%/35%
        self.assertEqual(comp_weights["required_skills"], 19.5)
        self.assertEqual(comp_weights["preferred_skills"], 10.5)
        self.assertEqual(comp_weights["experience"], 15.0)
        self.assertEqual(comp_weights["remote_location"], 15.0)
        self.assertEqual(comp_weights["salary"], 10.0)
        self.assertEqual(comp_weights["education"], 5.0)
        self.assertEqual(comp_weights["company_preference"], 2.5)
        self.assertEqual(comp_weights["industry"], 2.5)

    def test_summarize_weight_configuration(self):
        summary = summarize_weight_configuration(PRESET_TECHNICAL_SKILLS_FOCUS, preset_name="technical_skills")
        self.assertEqual(summary["preset_name"], "technical_skills")
        self.assertEqual(summary["total_weight"], 100.0)
        self.assertTrue(summary["is_normalized"])
        self.assertIn("required_skills", summary["component_weights"])


if __name__ == "__main__":
    unittest.main()
