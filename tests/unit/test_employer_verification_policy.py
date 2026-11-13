"""Strict preference and save-normalization checks; no provider or bootstrap execution."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from packages.domain.employer_verification import (
    DISABLED, GREENHOUSE_RECIPROCAL, EmployerVerificationPreference,
    disabled_preference, normalize_activation, verification_policy_snapshot,
)
from packages.domain.profile import JobPreferences


class EmployerVerificationPolicyTests(unittest.TestCase):
    def enabled(self, activation_id=None):
        return {"version": 1, "mode": GREENHOUSE_RECIPROCAL, "activation_id": activation_id}

    def test_default_is_disabled_and_nested_values_are_not_shared(self):
        first = JobPreferences()
        second = JobPreferences()
        self.assertEqual(first.company_discovery.employer_verification.model_dump(), disabled_preference())
        first.company_discovery.employer_verification.mode = GREENHOUSE_RECIPROCAL
        self.assertEqual(second.company_discovery.employer_verification.mode, DISABLED)
        value = disabled_preference()
        value["mode"] = GREENHOUSE_RECIPROCAL
        self.assertEqual(disabled_preference()["mode"], DISABLED)

    def test_model_rejects_coercion_unknown_modes_ids_and_fields(self):
        cases = [
            {"version": value} for value in (True, False, 1.0, "1", 0, 2, None)
        ] + [
            {"mode": value} for value in ("greenhouse", "MONITORING", True, 1, None)
        ] + [
            {"activation_id": value}
            for value in ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "\n", 1, True, [])
        ] + [{"rate_limit": 600}, {"provider": "lever"}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EmployerVerificationPreference.model_validate(value)

    def test_explicit_enable_mints_an_id_without_changing_inputs(self):
        incoming = self.enabled()
        original = deepcopy(incoming)
        with patch("packages.domain.employer_verification.uuid4", return_value=SimpleNamespace(hex="a" * 32)) as mint:
            actual = normalize_activation(incoming, company_discovery_enabled=True)
        mint.assert_called_once_with()
        self.assertEqual(actual, self.enabled("a" * 32))
        self.assertEqual(incoming, original)

    def test_active_save_preserves_id_for_echo_or_null_without_reminting(self):
        old = self.enabled("a" * 32)
        with patch("packages.domain.employer_verification.uuid4") as mint:
            for identifier in (None, "a" * 32):
                with self.subTest(identifier=identifier):
                    self.assertEqual(normalize_activation(self.enabled(identifier), old,
                        company_discovery_enabled=True), old)
        mint.assert_not_called()

    def test_foreign_or_historical_ids_cannot_create_or_replace_activation(self):
        for previous in (None, disabled_preference(), self.enabled("b" * 32)):
            with self.subTest(previous=previous), self.assertRaisesRegex(
                    ValueError, "^EMPLOYER_VERIFICATION_ACTIVATION_ID_CHANGED$"):
                normalize_activation(self.enabled("a" * 32), previous, company_discovery_enabled=True)

    def test_committed_disable_then_enable_mints_fresh_identity(self):
        old = self.enabled("a" * 32)
        disabled = normalize_activation(disabled_preference(), old, company_discovery_enabled=True)
        self.assertEqual(disabled, disabled_preference())
        with patch("packages.domain.employer_verification.uuid4", return_value=SimpleNamespace(hex="b" * 32)):
            current = normalize_activation(self.enabled(), disabled, company_discovery_enabled=True)
        self.assertEqual(current, self.enabled("b" * 32))
        self.assertNotEqual(current["activation_id"], old["activation_id"])

    def test_both_disable_controls_clear_a_valid_echoed_id(self):
        previous = self.enabled("a" * 32)
        for incoming, discovery in [
                ({"version": 1, "mode": DISABLED, "activation_id": "a" * 32}, True),
                (previous, False)]:
            with self.subTest(discovery=discovery):
                self.assertEqual(normalize_activation(incoming, previous,
                    company_discovery_enabled=discovery), disabled_preference())

    def test_malformed_previous_active_grant_cannot_be_reminted_but_can_be_revoked(self):
        for previous in (self.enabled(), {"mode": GREENHOUSE_RECIPROCAL, "activation_id": "bad"},
                         {"mode": "UNKNOWN"}):
            with self.subTest(previous=previous):
                with self.assertRaisesRegex(ValueError, "^EMPLOYER_VERIFICATION_AUTHORIZATION_INVALID$"):
                    normalize_activation(self.enabled(), previous, company_discovery_enabled=True)
                self.assertEqual(normalize_activation(disabled_preference(), previous,
                    company_discovery_enabled=True), disabled_preference())

    def test_normalizer_requires_strict_enabled_flag_and_valid_incoming_value(self):
        for enabled in (1, 0, "true", None):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                normalize_activation(self.enabled(), company_discovery_enabled=enabled)
        for value in (None, [], {"version": True}, {"mode": "UNKNOWN"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_activation(value, company_discovery_enabled=True)

    def test_snapshot_binds_exact_profile_and_fixed_separate_policy(self):
        profile = {"id": "profile-one", "version": "c" * 64, "preferences": {
            "company_discovery": {"company_discovery_enabled": True,
                "employer_verification": self.enabled("a" * 32)}}}
        original = deepcopy(profile)
        actual = verification_policy_snapshot(profile)
        self.assertEqual(actual, {
            "schema": "EMPLOYER_VERIFICATION_POLICY_V1", "version": 1,
            "mode": GREENHOUSE_RECIPROCAL, "profile_id": "profile-one",
            "profile_version": "c" * 64, "activation_id": "a" * 32,
            "native_provider": "greenhouse", "origin_scope": "PERSISTED_HINT_AND_RECIPROCAL_EMPLOYER",
            "rate_scope": "ONE_DISCOVERY_ITEM_SHARED_NATIVE_EMPLOYER",
            "request_timeout": 10, "rate_limit": 30, "concurrency": 1,
        })
        self.assertEqual(profile, original)

    def test_absent_disabled_or_discovery_disabled_snapshot_grants_nothing(self):
        for profile in ({}, {"preferences": {}}, {"preferences": {"company_discovery": {
                "employer_verification": disabled_preference()}}},
                {"preferences": {"company_discovery": {"company_discovery_enabled": False,
                    "employer_verification": self.enabled("a" * 32)}}}):
            with self.subTest(profile=profile):
                self.assertIsNone(verification_policy_snapshot(profile))

    def test_active_snapshot_requires_saved_identity_and_strict_profile_version(self):
        base = {"id": "profile-one", "version": "c" * 64, "preferences": {
            "company_discovery": {"employer_verification": self.enabled("a" * 32)}}}
        cases = []
        for key, values in (("id", (None, "", "../foreign", " ")), ("version", (None, True, "c" * 63, "C" * 64))):
            for value in values:
                candidate = deepcopy(base)
                candidate[key] = value
                cases.append(candidate)
        candidate = deepcopy(base)
        candidate["preferences"]["company_discovery"]["employer_verification"]["activation_id"] = None
        cases.append(candidate)
        candidate = deepcopy(base)
        candidate["preferences"]["company_discovery"]["company_discovery_enabled"] = 1
        cases.append(candidate)
        for profile in cases:
            with self.subTest(profile=profile), self.assertRaisesRegex(
                    ValueError, "^EMPLOYER_VERIFICATION_AUTHORIZATION_INVALID$"):
                verification_policy_snapshot(profile)
