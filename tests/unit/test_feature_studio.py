"""Feature Studio schema and safety regression checks."""
import copy
import json
from pathlib import Path

from tools.feature_studio.validate import STUDIO, check_against_schema, validate_config, validate_spec

SCHEMA = json.loads((STUDIO / "schema.v1.json").read_text(encoding="utf-8"))
RECORD = json.loads((STUDIO / "specs" / "FS-TEST-001.json").read_text(encoding="utf-8"))
CONFIG = json.loads((STUDIO / "config.json").read_text(encoding="utf-8"))


def test_smoke_fixture_is_valid_draft():
    assert validate_spec(RECORD, SCHEMA) == []
    assert RECORD["lifecycle"] == "DRAFT"
    assert all(item["status"] == "NOT_RUN" for item in RECORD["acceptance_criteria"])


def test_pilot_configuration_disallows_org_write_and_agent_execution():
    assert validate_config(CONFIG) == []
    assert not CONFIG["agent_execution_enabled"]
    assert not CONFIG["git_write_integration_enabled"]


def test_missing_outcome_is_rejected():
    record = copy.deepcopy(RECORD)
    record.pop("desired_outcome")
    assert any("desired_outcome" in error for error in validate_spec(record, SCHEMA))


def test_unknown_field_is_rejected():
    record = copy.deepcopy(RECORD)
    record["surprise"] = True
    assert any("surprise" in error for error in validate_spec(record, SCHEMA))


def test_false_feature_completion_is_rejected():
    record = copy.deepcopy(RECORD)
    record["lifecycle"] = "VERIFIED"
    record["execution"]["spec_review_approved"] = True
    assert any("every criterion PASS" in error for error in validate_spec(record, SCHEMA))


def test_pass_without_evidence_is_rejected():
    record = copy.deepcopy(RECORD)
    record["acceptance_criteria"][0]["status"] = "PASS"
    assert any("requires verifiable evidence" in error for error in validate_spec(record, SCHEMA))


def test_unapproved_agent_execution_is_rejected():
    record = copy.deepcopy(RECORD)
    record["execution"]["agent_execution_authorized"] = True
    assert any("agent authorization" in error for error in validate_spec(record, SCHEMA))


def test_automatic_main_push_is_rejected():
    record = copy.deepcopy(RECORD)
    record["execution"]["automated_main_push"] = True
    assert any("automatic main pushes" in error for error in validate_spec(record, SCHEMA))


def test_duplicate_acceptance_ids_are_rejected():
    record = copy.deepcopy(RECORD)
    record["acceptance_criteria"].append(copy.deepcopy(record["acceptance_criteria"][0]))
    assert any("duplicate" in error for error in validate_spec(record, SCHEMA))


def test_bad_repository_is_rejected():
    record = copy.deepcopy(RECORD)
    record["repository"] = "not-a-repo"
    assert any("repository" in error for error in validate_spec(record, SCHEMA))


def test_org_rollout_cannot_be_enabled_by_json_change():
    config = copy.deepcopy(CONFIG)
    config["org_rollout_approved"] = True
    assert validate_config(config)


def test_schema_rejects_wrong_type():
    assert check_against_schema("yes", {"type": "boolean"}) == ["$: expected boolean"]
