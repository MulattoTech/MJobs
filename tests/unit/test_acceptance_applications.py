"""Fail-closed contracts for MJ-REQ-292 application observation production."""

import json
import pytest

from cli.acceptance_applications import (
    APPLICATION_CHECKS,
    AssertionEvidence,
    record_application_step,
)


SOURCE_COMMIT = "a" * 40
RUN_ID = "application-run-1"
RUNTIME = {
    "source_manifest_sha256": "b" * 64,
    "images": {"api": "sha256:" + "c" * 64, "web": "sha256:" + "d" * 64},
}


class Journal:
    def __init__(self, *, run_id=RUN_ID, source_commit=SOURCE_COMMIT):
        self.acceptance_run_id = run_id
        self.binding = {"source": {"commit": source_commit}}
        self.selected_profile_id = "profile-B"
        self.selected_application_id = "app-B"
        self.selected_application_job_id = "job-B"
        self.application_document_ids = ["resume-B", "letter-B"]
        self.stage_lineage = []
        self.observations = []

    def record_stage_lineage(self, row):
        if any(item["step"] == row["step"] for item in self.stage_lineage):
            raise ValueError("duplicate stage lineage")
        self.stage_lineage.append(row)

    def record_observation(self, **observation):
        self.observations.append(observation)
        return observation


def lineage(step, *, kind=None, app="app-B", profile="profile-B", job="job-B"):
    if kind is None:
        kind = "PREPARE" if step in {36, 37} else "APPLY_MOCK"
    return {
        "step": step,
        "operation_id": "e" * 64,
        "request_id": f"request-{step}",
        "operation_kind": kind,
        "application_id": app,
        "profile_id": profile,
        "job_id": job,
    }


def assertions_for(step, *, ui=True, readback=True):
    return {name: AssertionEvidence(ui=ui, readback=readback)
            for name in APPLICATION_CHECKS[step]}


def record(journal, step, **overrides):
    args = {
        "observable_result": "Application step was observed and read back.",
        "assertions": assertions_for(step),
        "runtime_identity": RUNTIME,
        "stage_lineage": lineage(step),
    }
    args.update(overrides)
    return record_application_step(journal, step, **args)


def test_passing_step_requires_runtime_source_run_and_matching_application_lineage():
    journal = Journal()

    result = record(journal, 49)

    assert result["status"] == "PASS"
    assert result["evidence_kind"] == "CONTROLLED_FIXTURE"
    assert {row["name"] for row in result["checks"]} == APPLICATION_CHECKS[49]
    assert result["continuity"] == {
        "profile_id": "profile-B", "application_id": "app-B", "job_id": "job-B"}
    assert journal.stage_lineage == [lineage(49)]


def test_real_evidence_journal_persists_step_and_exact_stage_lineage(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    artifact_path = tmp_path / "ui-evidence.json"
    journal = EvidenceJournal(
        artifact_path,
        {"source": {"commit": SOURCE_COMMIT}, "runtime_identity": RUNTIME},
        acceptance_run_id=RUN_ID,
    )
    journal.record_application_readback(
        profile_id="profile-B", application_id="app-B", job_id="job-B",
        documents=[{"id": "resume-B"}, {"id": "letter-B"}])

    result = record_application_step(
        journal, 36,
        observable_result="Application preparation readback matched selected pins.",
        assertions=assertions_for(36),
        runtime_identity=RUNTIME,
        stage_lineage=lineage(36, kind="PREPARE"),
    )

    assert result["status"] == "PASS"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["acceptance_run_id"] == RUN_ID
    assert payload["runtime_identity"] == RUNTIME
    assert payload["selected_profile_id"] == "profile-B"
    assert payload["selected_application_id"] == "app-B"
    assert payload["selected_application_job_id"] == "job-B"
    assert payload["stage_lineage"] == [lineage(36, kind="PREPARE")]
    assert payload["acceptance_observations"][0] == result


@pytest.mark.parametrize("missing", ["runtime_identity", "stage_lineage"])
def test_missing_runtime_or_stage_ids_cannot_earn_pass(missing):
    journal = Journal()
    override = {missing: None}

    result = record(journal, 48, **override)

    assert result["status"] == "PARTIAL"
    assert not any(check["passed"] for check in result["checks"])
    assert bool(journal.stage_lineage) is (missing == "runtime_identity")


def test_missing_source_or_run_binding_leaves_step_unobserved():
    for journal in (Journal(run_id=None), Journal(source_commit="")):
        assert record(journal, 36) is None
        assert journal.observations == []
        assert journal.stage_lineage == []


def test_paired_ui_and_readback_assertions_are_required():
    journal = Journal()
    evidence = assertions_for(39)
    evidence[next(iter(evidence))] = AssertionEvidence(ui=True, readback=False)

    result = record(journal, 39, assertions=evidence)

    assert result["status"] == "PARTIAL"
    assert not all(check["passed"] for check in result["checks"])


def test_bare_boolean_check_flags_are_rejected():
    journal = Journal()
    flags = {name: True for name in APPLICATION_CHECKS[39]}

    with pytest.raises(ValueError, match="paired Playwright"):
        record(journal, 39, assertions=flags)


def test_contact_and_approved_answers_use_readback_checks_not_value_visibility():
    assert APPLICATION_CHECKS[40] == {
        "contact_fields_match_verified_profile", "contact_fields_readback_verified"}
    assert APPLICATION_CHECKS[43] == {
        "approved_answers_match_questions", "approved_answers_readback_verified"}


def test_stale_or_cross_application_lineage_cannot_earn_pass():
    journal = Journal()
    other = lineage(44, app="app-A")

    result = record(journal, 44, stage_lineage=other)

    assert result["status"] == "PARTIAL"
    assert not any(check["passed"] for check in result["checks"])
    assert journal.stage_lineage == []


@pytest.mark.parametrize("step", range(39, 44))
def test_native_form_steps_reject_prepare_operation_kind(step):
    journal = Journal()

    result = record(journal, step, stage_lineage=lineage(step, kind="PREPARE"))

    assert result["status"] == "PARTIAL"
    assert journal.stage_lineage == []


@pytest.mark.parametrize("step", [36, 37])
def test_prepare_steps_reject_apply_mock_operation_kind(step):
    journal = Journal()

    result = record(journal, step, stage_lineage=lineage(step, kind="APPLY_MOCK"))

    assert result["status"] == "PARTIAL"
    assert journal.stage_lineage == []


def test_document_upload_steps_require_selected_document_identity():
    journal = Journal()

    result = record(journal, 41, document_id="other-resume")

    assert result["status"] == "PARTIAL"
    assert "document_id" not in result["continuity"]


@pytest.mark.parametrize("step,document", [(41, "resume-B"), (42, "letter-B")])
def test_upload_step_binds_the_selected_document(step, document):
    journal = Journal()

    result = record(journal, step, document_id=document)

    assert result["status"] == "PASS"
    assert result["continuity"]["document_id"] == document


@pytest.mark.parametrize("runtime", [
    None,
    {"source_manifest_sha256": "b" * 64, "images": {"web": "sha256:" + "d" * 64}},
    {"source_manifest_sha256": "b" * 64, "images": {"api": ""}},
])
def test_missing_runtime_binding_fields_force_partial(runtime):
    journal = Journal()

    result = record(journal, 36, runtime_identity=runtime)

    assert result["status"] == "PARTIAL"
    assert not any(check["passed"] for check in result["checks"])


def test_untrusted_or_unbounded_observable_result_is_rejected():
    journal = Journal()

    with pytest.raises(ValueError, match="sanitized"):
        record(journal, 36, observable_result="Submitted to https://real-employer.example")


def test_observable_result_rejects_leading_or_trailing_whitespace():
    journal = Journal()

    with pytest.raises(ValueError, match="sanitized"):
        record(journal, 36, observable_result="Application result has trailing space ")
