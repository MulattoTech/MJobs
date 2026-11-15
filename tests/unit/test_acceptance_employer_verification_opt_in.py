"""Controlled UI-witness validation; these fixtures grant no runtime authority."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from cli import ui_e2e
from packages.domain.employer_verification import verification_policy_snapshot


_CHECKS = (
    "default_disabled", "unsaved_checkbox_did_not_activate", "normal_save_observed",
    "expected_version_matched", "client_activation_absent", "response_and_readback_matched",
    "saved_control_checked", "same_activation_at_baseline", "baseline_version_matched",
)
_SOURCE = {"commit": "a" * 40, "tree": "b" * 40,
           "working_tree_diff_sha256": hashlib.sha256(b"").hexdigest(),
           "working_tree_status_sha256": hashlib.sha256(b"").hexdigest()}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _controlled_profile(*, profile_id="profile-1", version="3" * 64,
                        activation_id="a" * 32):
    """Pure synthetic input for validator tests, never persisted or dispatched."""
    return {"id": profile_id, "version": version, "preferences": {"company_discovery": {
        "company_discovery_enabled": True,
        "employer_verification": {"version": 1, "mode": "GREENHOUSE_RECIPROCAL_V1",
                                  "activation_id": activation_id}}}}


def _controlled_ui_artifact(*, profile=None, source=None, run_id="opt-in-controlled-run",
                            origin="http://127.0.0.1:18777", inventory=None):
    """Construct only a controlled witness; connected Save is tested separately."""
    profile = copy.deepcopy(profile if profile is not None else _controlled_profile())
    source = copy.deepcopy(source if source is not None else _SOURCE)
    moment = datetime.now(timezone.utc).isoformat()
    saved_profile = {**profile, "version": "2" * 64}
    policy, saved_policy = verification_policy_snapshot(profile), verification_policy_snapshot(saved_profile)
    witness = {
        "schema_version": 1, "scope": "PROFILE_UI_SAVE_ONLY", "result": "BOUND_TO_UI_BASELINE",
        "profile_id": profile["id"], "before_profile_version": "1" * 64,
        "saved_profile_version": saved_profile["version"], "profile_version": profile["version"],
        "activation_id_sha256": _digest(policy["activation_id"]),
        "saved_policy_sha256": _digest(saved_policy), "policy_sha256": _digest(policy),
        "saved_at_utc": moment, "baseline_bound_at_utc": moment,
        "source": source, "acceptance_run_id": run_id, "target_origin": origin,
        "checks": {name: True for name in _CHECKS},
    }
    return {
        "profile_id": profile["id"], "source": source, "acceptance_run_id": run_id,
        "target_origin": origin, "mode": "ONBOARDING_FIXTURE", "timestamp": moment,
        "external_submission_enabled": False,
        "job_inventory": copy.deepcopy(inventory if inventory is not None else {
            "profile_id": profile["id"], "profile_version": profile["version"]}),
        "employer_verification_opt_in": witness,
    }


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_non_boolean_opt_in_rejects_before_preflight_or_output(tmp_path, monkeypatch, value):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("No preflight, output, browser or Save is allowed")

    for name in ("_onboarding_preflight", "prepare_ui_artifact_directory", "sync_playwright",
                 "observe_employer_verification_opt_in"):
        monkeypatch.setattr(ui_e2e, name, forbidden)
    with pytest.raises(ValueError, match="boolean"):
        ui_e2e.run("http://127.0.0.1:18777", tmp_path / "ui.json", onboarding=True,
                   acceptance_run_id="opt-in-controlled-run", employer_verification=value)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("onboarding,run_id", [
    (False, "opt-in-controlled-run"), (1, "opt-in-controlled-run"),
    (True, None), (True, ""), (True, "unsafe run"), (True, "../escape"), (True, 1),
])
def test_opt_in_requires_a_valid_run_bound_onboarding_before_io(
        tmp_path, monkeypatch, onboarding, run_id):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Invalid opt-in must not perform I/O")

    for name in ("_onboarding_preflight", "prepare_ui_artifact_directory", "sync_playwright"):
        monkeypatch.setattr(ui_e2e, name, forbidden)
    with pytest.raises(ValueError):
        ui_e2e.run("http://127.0.0.1:18777", tmp_path / "ui.json", onboarding=onboarding,
                   acceptance_run_id=run_id, employer_verification=True)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_default_witness_gate_neither_requires_nor_creates_an_activation():
    historical = {"source": copy.deepcopy(_SOURCE)}
    before = copy.deepcopy(historical)
    assert ui_e2e.validate_employer_verification_witness(historical, False) is None
    assert historical == before
    with pytest.raises(ValueError, match="explicit"):
        ui_e2e.validate_employer_verification_witness(_controlled_ui_artifact(), False)


def test_controlled_bound_witness_is_read_only_and_has_no_numbered_observation():
    profile = _controlled_profile()
    artifact = _controlled_ui_artifact(profile=profile)
    before = copy.deepcopy((artifact, profile))
    assert ui_e2e.validate_employer_verification_witness(artifact, True, current_profile=profile) is None
    assert (artifact, profile) == before
    assert artifact["employer_verification_opt_in"]["scope"] == "PROFILE_UI_SAVE_ONLY"
    assert "acceptance_observations" not in artifact
    assert "activation_id" not in artifact["employer_verification_opt_in"]


@pytest.mark.parametrize("change", [
    "missing-witness", "null-witness", "missing-field", "extra-field", "pending-result",
    "schema-bool", "schema-string", "scope", "missing-check", "false-check", "integer-check",
    "check-list", "profile", "source", "run", "origin", "mode", "external-true",
    "external-zero", "inventory-null", "inventory-profile", "inventory-version", "bad-digest",
    "same-save-version", "absent-source", "absent-profile", "empty-origin",
])
def test_controlled_witness_shape_and_identity_fail_closed(change):
    artifact = _controlled_ui_artifact()
    witness = artifact["employer_verification_opt_in"]
    if change == "missing-witness": artifact.pop("employer_verification_opt_in")
    elif change == "null-witness": artifact["employer_verification_opt_in"] = None
    elif change == "missing-field": witness.pop("saved_policy_sha256")
    elif change == "extra-field": witness["numbered_acceptance"] = "PASS"
    elif change == "pending-result": witness["result"] = "SAVED_PENDING_BASELINE"
    elif change == "schema-bool": witness["schema_version"] = True
    elif change == "schema-string": witness["schema_version"] = "1"
    elif change == "scope": witness["scope"] = "LIVE_PUBLIC_READ_ONLY"
    elif change == "missing-check": witness["checks"].pop("normal_save_observed")
    elif change == "false-check": witness["checks"]["normal_save_observed"] = False
    elif change == "integer-check": witness["checks"]["normal_save_observed"] = 1
    elif change == "check-list": witness["checks"] = []
    elif change == "profile": witness["profile_id"] = "another-profile"
    elif change == "source": witness["source"] = {**_SOURCE, "commit": "f" * 40}
    elif change == "run": witness["acceptance_run_id"] = "another-run"
    elif change == "origin": witness["target_origin"] = "http://127.0.0.1:18778"
    elif change == "mode": artifact["mode"] = "FICTIONAL_DEMO"
    elif change == "external-true": artifact["external_submission_enabled"] = True
    elif change == "external-zero": artifact["external_submission_enabled"] = 0
    elif change == "inventory-null": artifact["job_inventory"] = None
    elif change == "inventory-profile": artifact["job_inventory"]["profile_id"] = "another-profile"
    elif change == "inventory-version": artifact["job_inventory"]["profile_version"] = "4" * 64
    elif change == "bad-digest": witness["saved_policy_sha256"] = "not-a-sha256"
    elif change == "same-save-version": witness["saved_profile_version"] = witness["before_profile_version"]
    elif change == "absent-source": artifact["source"] = witness["source"] = None
    elif change == "absent-profile": artifact["profile_id"] = witness["profile_id"] = None
    elif change == "empty-origin": artifact["target_origin"] = witness["target_origin"] = ""
    with pytest.raises(ValueError):
        ui_e2e.validate_employer_verification_witness(artifact, True)


@pytest.mark.parametrize("change", ["naive-save", "invalid-save", "save-after-bind", "bind-after-finish"])
def test_controlled_witness_chronology_is_ordered_and_timezone_aware(change):
    artifact = _controlled_ui_artifact()
    witness = artifact["employer_verification_opt_in"]
    moment = datetime.fromisoformat(artifact["timestamp"])
    if change == "naive-save": witness["saved_at_utc"] = moment.replace(tzinfo=None).isoformat()
    elif change == "invalid-save": witness["saved_at_utc"] = "not-a-timestamp"
    elif change == "save-after-bind": witness["saved_at_utc"] = (moment + timedelta(seconds=1)).isoformat()
    elif change == "bind-after-finish": witness["baseline_bound_at_utc"] = (moment + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="chronology"):
        ui_e2e.validate_employer_verification_witness(artifact, True)


def _pending_save_witness():
    witness = _controlled_ui_artifact()["employer_verification_opt_in"]
    for key in ("profile_version", "policy_sha256", "baseline_bound_at_utc"):
        witness.pop(key)
    witness["result"] = "SAVED_PENDING_BASELINE"
    witness["checks"] = {name: True for name in _CHECKS[:7]}
    return witness


def test_later_profile_version_keeps_activation_and_binds_exact_inventory():
    witness = _pending_save_witness()
    profile = _controlled_profile(version="4" * 64)
    inventory = {"profile_id": profile["id"], "profile_version": profile["version"]}
    before = copy.deepcopy((witness, profile, inventory))
    bound = ui_e2e.bind_employer_verification_baseline(witness, profile, inventory)
    assert (witness, profile, inventory) == before
    assert bound["result"] == "BOUND_TO_UI_BASELINE"
    assert bound["saved_profile_version"] == "2" * 64
    assert bound["profile_version"] == "4" * 64
    assert bound["activation_id_sha256"] == witness["activation_id_sha256"]
    assert bound["policy_sha256"] == _digest(verification_policy_snapshot(profile))
    assert bound["checks"] == {name: True for name in _CHECKS}
    # Content versions can change; this makes no monotonic edit-generation claim.


@pytest.mark.parametrize("change", ["revoked", "parent-disabled", "rotated", "profile", "inventory-profile", "inventory-version"])
def test_baseline_binding_rejects_revocation_rotation_or_profile_inventory_drift(change):
    witness, profile = _pending_save_witness(), _controlled_profile()
    inventory = {"profile_id": profile["id"], "profile_version": profile["version"]}
    preferences = profile["preferences"]["company_discovery"]
    if change == "revoked": preferences["employer_verification"] = {"version": 1, "mode": "DISABLED", "activation_id": None}
    elif change == "parent-disabled": preferences["company_discovery_enabled"] = False
    elif change == "rotated": preferences["employer_verification"]["activation_id"] = "b" * 32
    elif change == "profile": profile["id"] = "restored-profile"
    elif change == "inventory-profile": inventory["profile_id"] = "restored-profile"
    elif change == "inventory-version": inventory["profile_version"] = "4" * 64
    with pytest.raises(ValueError):
        ui_e2e.bind_employer_verification_baseline(witness, profile, inventory)


@pytest.mark.parametrize("change", ["version", "activation", "profile", "revoked", "parent-disabled", "extra-policy-field"])
def test_current_profile_readback_cannot_reuse_a_stale_controlled_witness(change):
    artifact, current = _controlled_ui_artifact(), _controlled_profile()
    preferences = current["preferences"]["company_discovery"]
    if change == "version": current["version"] = "4" * 64
    elif change == "activation": preferences["employer_verification"]["activation_id"] = "b" * 32
    elif change == "profile": current["id"] = "restored-profile"
    elif change == "revoked": preferences["employer_verification"] = {"version": 1, "mode": "DISABLED", "activation_id": None}
    elif change == "parent-disabled": preferences["company_discovery_enabled"] = False
    elif change == "extra-policy-field": preferences["employer_verification"]["rate_limit"] = 1000
    with pytest.raises(ValueError):
        ui_e2e.validate_employer_verification_witness(artifact, True, current_profile=current)
