"""Route authority requires persisted identity evidence, not a provider label."""
import unittest
from pydantic import ValidationError

from packages.domain.profile import JobPreferences
from services.dedupe.routes import route_authority


class ApplicationRouteAuthorityTests(unittest.TestCase):
    def test_profile_route_preference_defaults_to_verified_and_rejects_unknown_policies(self):
        self.assertEqual(JobPreferences().application_route_preference, "PREFER_VERIFIED")
        self.assertEqual(JobPreferences.model_validate({
            "application_route_preference": "USER_CHOICE",
        }).application_route_preference, "USER_CHOICE")
        with self.assertRaises(ValidationError):
            JobPreferences.model_validate({"application_route_preference": "ALWAYS_BYPASS"})

    def test_known_ats_provider_without_verified_tenant_evidence_is_unknown(self):
        self.assertEqual(route_authority({"provider": "greenhouse", "tenant": "example"}), "UNKNOWN")

    def test_explicit_internal_evidence_projection_sets_verified_authority(self):
        self.assertEqual(route_authority({
            "provider": "greenhouse", "tenant": "example", "authority": "VERIFIED_ATS",
        }), "VERIFIED_ATS")


if __name__ == "__main__":
    unittest.main()
