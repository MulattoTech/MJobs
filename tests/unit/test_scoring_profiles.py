"""Data-driven role-family profile selection preserves the legacy default."""
import unittest

from services.discovery.matching import build_explanation
from services.matching.scoring_profiles import resolve_role_scoring_profile


class RoleScoringProfileResolutionTests(unittest.TestCase):
    @staticmethod
    def _profile(profile_id, version, role_family, weights, thresholds=None):
        return {
            "id": profile_id,
            "version": version,
            "schema_version": 1,
            "role_family": role_family,
            "weights": weights,
            "thresholds": thresholds or {},
        }

    def test_configured_families_select_distinct_user_profiles_without_occupation_rules(self):
        configured = {
            "Cloud Operations": self._profile(
                "cloud", "v1", "cloud operations", {"skills": 20, "title": 80}),
            "Systems Administration": self._profile(
                "systems", "v3", "systems administration", {"skills": 90, "title": 10}),
        }

        cloud = resolve_role_scoring_profile("  CLOUD   operations ", configured)
        systems = resolve_role_scoring_profile("systems administration", configured)

        self.assertEqual(cloud.profile["id"], "cloud")
        self.assertEqual(cloud.profile["weights"], {"skills": 20.0, "title": 80.0})
        self.assertEqual(systems.profile["id"], "systems")
        self.assertEqual(systems.profile["weights"], {"skills": 90.0, "title": 10.0})
        self.assertEqual(cloud.selection, "ROLE_FAMILY")
        self.assertEqual(systems.selection, "ROLE_FAMILY")
        self.assertNotEqual(cloud.profile["version"], systems.profile["version"])

    def test_resolution_explanation_identifies_the_selected_profile_and_source(self):
        selected = resolve_role_scoring_profile(
            "Cloud Operations",
            {"cloud operations": self._profile(
                "cloud-profile", "opaque-v4", "Cloud Operations",
                {"skills": 60}, {"title": 75})},
        )

        self.assertEqual(selected.explanation, {
            "requested_role_family": "cloud operations",
            "selected_role_family": "Cloud Operations",
            "profile_id": "cloud-profile",
            "profile_version": "opaque-v4",
            "selection": "ROLE_FAMILY",
        })

    def test_unknown_and_missing_families_use_an_explicit_configured_default(self):
        default = self._profile(
            "general-default", "v2", "General", {"skills": 100})
        configured = {
            "cloud": self._profile("cloud", "v1", "Cloud", {"title": 100}),
        }

        unknown = resolve_role_scoring_profile(
            "new and unsupported family", configured, default_profile=default)
        missing = resolve_role_scoring_profile(
            None, configured, default_profile=default)

        self.assertEqual(unknown.profile["id"], "general-default")
        self.assertEqual(missing.profile["id"], "general-default")
        self.assertEqual(unknown.selection, "DEFAULT_PROFILE")
        self.assertEqual(unknown.explanation["requested_role_family"],
                         "new and unsupported family")
        self.assertEqual(unknown.explanation["profile_version"], "v2")

    def test_no_configured_or_default_profile_preserves_legacy_scoring(self):
        job = {
            "title": "Systems Administrator",
            "skills": ["Linux", "Python"],
            "attributes": {
                "skill_evidence": "structured_required_skills",
                "required_skills": ["Linux", "Python"],
            },
        }
        candidate = {
            "facts": [{
                "kind": "skill", "text": "Linux", "source": "Approved profile",
                "verified": True, "user_approved": True, "confidence": 95,
            }],
            "preferences": {},
        }

        resolution = resolve_role_scoring_profile("new family", {})
        missing_catalog = resolve_role_scoring_profile("new family")
        legacy_result = build_explanation(job, candidate)
        resolved_result = (build_explanation(job, candidate, resolution.profile)
                           if resolution.profile is not None
                           else build_explanation(job, candidate))

        self.assertIsNone(resolution.profile)
        self.assertEqual(resolution.selection, "LEGACY_DEFAULT")
        self.assertIsNone(missing_catalog.profile)
        self.assertEqual(missing_catalog.selection, "LEGACY_DEFAULT")
        self.assertEqual(resolved_result, legacy_result)

    def test_profile_dimensions_are_data_driven_and_sparse_maps_are_preserved(self):
        profile = self._profile(
            "future", "v1", "Future Role Family",
            {"emerging_signal": 42}, {"emerging_signal": 75})

        resolved = resolve_role_scoring_profile(
            "future role family", {"future role family": profile})

        self.assertEqual(resolved.profile["weights"], {"emerging_signal": 42.0})
        self.assertEqual(resolved.profile["thresholds"], {"emerging_signal": 75.0})

    def test_invalid_catalog_or_profile_data_fails_with_a_stable_error(self):
        valid = self._profile("valid", "v1", "Cloud", {"skills": 50})
        invalid_profiles = [
            {"cloud": {**valid, "schema_version": True}},
            {"cloud": {**valid, "role_family": "Systems"}},
            {"cloud": {**valid, "eligibility_policy": {"exclude": ["x"]}}},
            {"cloud": {**valid, "weights": {"": 5}}},
            {"cloud": {**valid, "weights": {"skills": True}}},
            {"cloud": {**valid, "weights": {"skills": -0.1}}},
            {"cloud": {**valid, "weights": {"skills": 100.1}}},
            {"cloud": {**valid, "weights": {"skills": float("nan")}}},
            {"cloud": {**valid, "thresholds": {"title": float("inf")}}},
            {"cloud": {**valid, "thresholds": {"title": 10 ** 1000}}},
            {"cloud": valid, " CLOUD ": valid},
        ]

        for catalog in invalid_profiles:
            with self.subTest(catalog=catalog):
                with self.assertRaisesRegex(ValueError, "ROLE_SCORING_PROFILE_INVALID"):
                    resolve_role_scoring_profile("cloud", catalog)

        with self.assertRaisesRegex(ValueError, "ROLE_SCORING_PROFILE_INVALID"):
            resolve_role_scoring_profile("cloud", [])

    def test_malformed_default_is_rejected_instead_of_silently_falling_back(self):
        valid = self._profile("default", "v1", "General", {"skills": 100})

        with self.assertRaisesRegex(ValueError, "ROLE_SCORING_PROFILE_INVALID"):
            resolve_role_scoring_profile(
                "unknown", {}, default_profile={**valid, "weights": {"skills": -1}})


if __name__ == "__main__":
    unittest.main()
