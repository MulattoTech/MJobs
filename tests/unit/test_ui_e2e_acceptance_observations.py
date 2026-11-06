"""Safety and lineage checks for numbered UI acceptance observations."""

from pathlib import Path
import hashlib

import pytest

from cli.ui_e2e_jobs_journey import build_observation


RUN_ID = "01a1b028-0834-7130-b5ac-b7625e151716"
SOURCE_COMMIT = "7caf4e380bc8b5dc75024895ab400e770015a3ec"


def test_observation_has_exact_runner_contract_shape_and_matching_run_id():
    observation = build_observation(
        step=12,
        status="PASS",
        observable_result="Jobs search narrowed the visible list.",
        evidence_kind="CONTROLLED_FIXTURE",
        source_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        continuity={"profile_id": "profile-123"},
        checks=[{"name": "matching_job_visible", "passed": True},
                {"name": "nonmatching_job_hidden", "passed": True}],
    )

    assert set(observation) == {
        "step", "status", "observable_result", "evidence_kind",
        "source_commit", "run_id", "continuity", "checks",
    }
    assert observation["run_id"] == RUN_ID
    assert observation["step"] == 12
    assert observation["status"] == "PASS"


def test_pass_requires_nonempty_successful_assertions():
    with pytest.raises(ValueError, match="PASS requires"):
        build_observation(
            step=12, status="PASS", observable_result="Search checked.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "matching_job_visible", "passed": False},
                    {"name": "nonmatching_job_hidden", "passed": True}],
        )


def test_fixture_observation_cannot_pass_real_provider_discovery_step_11():
    with pytest.raises(ValueError, match="step 11 requires PUBLIC_READ_ONLY"):
        build_observation(
            step=11, status="PASS", observable_result="Provider jobs were seen.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "same_profile_public_readonly_jobs_observed", "passed": True}],
        )


def test_observation_rejects_unbounded_or_sensitive_fields():
    with pytest.raises(ValueError, match="safe identifier"):
        build_observation(
            step=12, status="PASS", observable_result="Search checked.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "https://example.invalid/private", "passed": True}],
        )

    with pytest.raises(ValueError, match="continuity"):
        build_observation(
            step=12, status="PASS", observable_result="Search checked.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            continuity={"profile_id": "profile-123", "job_title": "Private data"},
            checks=[{"name": "matching_job_visible", "passed": True},
                    {"name": "nonmatching_job_hidden", "passed": True}],
        )


def test_observation_checks_must_match_the_registered_step_names():
    with pytest.raises(ValueError, match="registered check names"):
        build_observation(
            step=12, status="PASS", observable_result="Search checked.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "arbitrary_assertion", "passed": True}],
        )

    with pytest.raises(ValueError, match="registered check names"):
        build_observation(
            step=6, status="PARTIAL", observable_result="A fact was checked.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "corrected_fact_read_back", "passed": True}],
        )

    with pytest.raises(ValueError, match="registered check names"):
        build_observation(
            step=14, status="PASS", observable_result="Jobs follow match-score order.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "title_sort_order", "passed": True}],
        )

    assert build_observation(
        step=14, status="PASS", observable_result="Jobs follow match-score order.",
        evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        checks=[{"name": "match_sort_order", "passed": True}],
    )["step"] == 14


def test_observation_accepts_job_and_document_lineage_with_bounded_lists():
    observation = build_observation(
        step=34, status="PASS", observable_result="Base and tailored versions compared.",
        evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT, run_id=RUN_ID,
        continuity={"profile_id": "profile-123", "application_id": "app-456",
                    "job_id": "job-789", "document_ids": ["doc-base", "doc-tailored"]},
        checks=[{"name": "generated_base_visible", "passed": True},
                {"name": "tailored_version_visible", "passed": True},
                {"name": "omission_and_change_summary_visible", "passed": True}],
    )
    assert observation["continuity"]["document_ids"] == ["doc-base", "doc-tailored"]

    with pytest.raises(ValueError, match="document_ids"):
        build_observation(
            step=34, status="PASS", observable_result="Base and tailored versions compared.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT, run_id=RUN_ID,
            continuity={"document_ids": ["same", "same"]},
            checks=[{"name": "generated_base_visible", "passed": True},
                    {"name": "tailored_version_visible", "passed": True},
                    {"name": "omission_and_change_summary_visible", "passed": True}],
        )


def test_step_22_requires_visible_numeric_score_and_selected_job_explanation():
    observation = build_observation(
        step=22, status="PASS", observable_result="Selected job score and requirement explanation are visible.",
        evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT, run_id=RUN_ID,
        continuity={"profile_id": "profile-123", "job_id": "job-789"},
        checks=[{"name": "visible_numeric_match_score", "passed": True},
                {"name": "selected_job_requirement_explanation_visible", "passed": True}],
    )
    assert observation["status"] == "PASS"


def test_step_3_requires_first_run_wizard_readback_check():
    observation = build_observation(
        step=3, status="PASS", observable_result="Wizard configuration and search were reloaded.",
        evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT, run_id=RUN_ID,
        continuity={"profile_id": "profile-123"},
        checks=[{"name": "first_run_wizard_completed_with_persisted_configuration",
                 "passed": True}],
    )
    assert observation["checks"][0]["passed"] is True


def test_steps_36_to_53_have_fixed_matching_journal_check_registry():
    from cli.acceptance_applications import APPLICATION_CHECKS
    from cli.ui_e2e_jobs_journey import REQUIRED_CHECKS_BY_STEP, _SUPPORTED_STEP_IDS

    assert set(range(36, 54)) <= _SUPPORTED_STEP_IDS
    assert {step: REQUIRED_CHECKS_BY_STEP[step] for step in range(36, 54)} == APPLICATION_CHECKS


def test_steps_23_to_31_match_employer_producer_registry():
    from cli.acceptance_employer import REQUIRED_CHECKS_BY_STEP as EMPLOYER_CHECKS
    from cli.ui_e2e_jobs_journey import REQUIRED_CHECKS_BY_STEP, _SUPPORTED_STEP_IDS

    assert set(range(23, 32)) <= _SUPPORTED_STEP_IDS
    assert {step: REQUIRED_CHECKS_BY_STEP[step] for step in range(23, 32)} == EMPLOYER_CHECKS


def test_application_helper_records_through_real_evidence_journal(tmp_path):
    from cli.acceptance_applications import AssertionEvidence, record_application_step
    from cli.ui_e2e import EvidenceJournal
    from cli.ui_e2e_jobs_journey import REQUIRED_CHECKS_BY_STEP

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}},
        acceptance_run_id=RUN_ID)
    journal.record_application_readback(
        profile_id="profile-B", application_id="app-B", job_id="job-B",
        documents=[{"id": "doc-B"}])
    lineage = {"step": 36, "operation_id": "a" * 64,
               "request_id": "prepare-36", "operation_kind": "PREPARE",
               "application_id": "app-B", "profile_id": "profile-B",
               "job_id": "job-B"}
    result = record_application_step(
        journal, 36, observable_result="B application preparation was read back.",
        assertions={name: AssertionEvidence(ui=True, readback=True)
                    for name in REQUIRED_CHECKS_BY_STEP[36]},
        runtime_identity={"source_manifest_sha256": "b" * 64,
                          "images": {"api": "sha256:" + "c" * 64}},
        stage_lineage=lineage)

    assert result["status"] == "PASS"
    assert journal.payload()["stage_lineage"] == [lineage]
    assert journal.payload()["acceptance_observations"][0]["continuity"] == {
        "profile_id": "profile-B", "application_id": "app-B", "job_id": "job-B"}


@pytest.mark.parametrize("rendered, expected, matches", [
    ("100% · Exceptional", 100.0, True),
    ("83.5% · Excellent", 83.5, True),
    ("99% · Exceptional", 100.0, False),
    ("Unscored", 0, False),
])
def test_visible_numeric_score_matches_react_format(rendered, expected, matches):
    from cli.ui_e2e_jobs_journey import visible_numeric_match_score

    assert visible_numeric_match_score(rendered, expected) is matches


@pytest.mark.parametrize("detail, before, after, expected", [
    ({"id": "job-A", "match": None}, "version-1", "version-1", False),
    ({"id": "job-A", "match": {"score": 100}}, "version-1", "version-2", False),
    ({"id": "job-other", "match": {"score": 100}}, "version-1", "version-1", False),
    ({"id": "job-A", "match": {"score": 100}}, "version-1", "version-1", True),
])
def test_selected_job_match_readback_is_bound_to_stable_profile_version(
        detail, before, after, expected):
    from cli.ui_e2e_jobs_journey import current_match_readback

    assert current_match_readback(
        detail, job_id="job-A", profile_version_before=before,
        profile_version_after=after) is expected


def test_evidence_journal_records_actual_readback_lineage(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}}, acceptance_run_id=RUN_ID)
    journal.record_observed_jobs(["job-A"])
    journal.record_application_readback(
        profile_id="profile-B", application_id="app-B", job_id="job-B",
        documents=[{"id": "doc-1"}, {"id": "doc-2"}])
    payload = journal.payload()
    assert payload["observed_job_ids"] == ["job-A"]
    assert payload["selected_profile_id"] == "profile-B"
    assert payload["selected_application_job_id"] == "job-B"
    assert payload["selected_application_id"] == "app-B"
    assert payload["application_document_ids"] == ["doc-1", "doc-2"]


@pytest.mark.parametrize("profile, application, matches", [
    ({"id": "profile-1", "version": "version-1"},
     {"profile_id": "profile-1", "profile_version": "version-2"}, False),
    ({"id": "profile-1", "version": "version-1"},
     {"profile_id": "profile-2", "profile_version": "version-1"}, False),
    ({"id": "profile-1", "version": "version-1"},
     {"profile_id": "profile-1", "profile_version": "version-1"}, True),
])
def test_application_profile_version_binding_rejects_stale_profile_snapshot(
        profile, application, matches):
    from cli.ui_e2e import application_profile_version_matches

    assert application_profile_version_matches(profile, application) is matches


def test_mock_run_request_identity_is_bound_when_operation_params_omit_request_id():
    from cli.ui_e2e import assert_mock_run_request_lineage
    from packages.persistence.store import identity

    profile_id = "profile-1"
    application_id = "application-1"
    request_id = "reviewed-run-1"
    operation_id = identity(
        "application-run-request-v1", profile_id, application_id, request_id)
    operation = {"id": operation_id, "kind": "APPLY_MOCK",
                 "entity_id": application_id, "params": {"submit": False}}
    readback = {"operation_id": operation_id, "state": "QUEUED"}

    assert_mock_run_request_lineage(
        operation, readback, profile_id=profile_id,
        application_id=application_id, request_id=request_id)


def test_mock_run_request_lineage_rejects_conflicting_param_or_scoped_readback():
    from cli.ui_e2e import assert_mock_run_request_lineage
    from packages.persistence.store import identity

    profile_id = "profile-1"
    application_id = "application-1"
    request_id = "reviewed-run-1"
    operation_id = identity(
        "application-run-request-v1", profile_id, application_id, request_id)
    operation = {"id": operation_id, "kind": "APPLY_MOCK",
                 "entity_id": application_id,
                 "params": {"request_id": "different-run"}}
    with pytest.raises(AssertionError):
        assert_mock_run_request_lineage(
            operation, {"operation_id": operation_id}, profile_id=profile_id,
            application_id=application_id, request_id=request_id)

    operation["params"] = {"submit": False}
    with pytest.raises(AssertionError):
        assert_mock_run_request_lineage(
            operation, {"operation_id": "b" * 64}, profile_id=profile_id,
            application_id=application_id, request_id=request_id)


def test_employer_discovery_lineage_is_persisted_only_for_observed_jobs(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}},
        acceptance_run_id=RUN_ID)
    journal.record_observed_jobs(["job-A", "job-B"])
    lineage = {"profile_id": "profile-1", "operation_id": "d" * 64,
               "request_id": "request-discovery", "job_ids": ["job-A", "job-B"]}

    journal.record_employer_discovery_lineage(lineage)

    assert journal.payload()["employer_discovery_lineage"] == lineage
    with pytest.raises(ValueError, match="same-profile readback"):
        journal.record_employer_discovery_lineage({
            **lineage, "job_ids": ["job-A", "job-unobserved"]})
    with pytest.raises(ValueError, match="changed during journey"):
        journal.record_employer_discovery_lineage({
            **lineage, "request_id": "different-request"})


@pytest.mark.parametrize("lineage", [
    {"profile_id": "profile-1", "operation_id": "D" * 64,
     "request_id": "request-1", "job_ids": ["job-1"]},
    {"profile_id": "profile-1", "operation_id": "d" * 64,
     "request_id": "request-1", "job_ids": ["job-1", "job-1"]},
    {"profile_id": "profile-1", "operation_id": "d" * 64,
     "request_id": "request-1", "job_ids": []},
])
def test_employer_discovery_lineage_rejects_malformed_identity(tmp_path, lineage):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}},
        acceptance_run_id=RUN_ID)
    with pytest.raises(ValueError, match="invalid"):
        journal.record_employer_discovery_lineage(lineage)


def test_acceptance_artifacts_use_run_evidence_directory_and_refuse_overwrite(tmp_path):
    from cli.ui_e2e import prepare_ui_artifact_directory

    first_evidence = tmp_path / "run-one" / "ui.json"
    second_evidence = tmp_path / "run-two" / "ui.json"
    first = prepare_ui_artifact_directory(
        "http://127.0.0.1:4321", first_evidence, RUN_ID)
    second = prepare_ui_artifact_directory(
        "http://127.0.0.1:4321", second_evidence, "run-two-id")
    first_suffix = hashlib.sha256(RUN_ID.encode()).hexdigest()[:12]
    second_suffix = hashlib.sha256("run-two-id".encode()).hexdigest()[:12]
    assert first == first_evidence.parent / f"ui-{first_suffix}-artifacts"
    assert second == second_evidence.parent / f"ui-{second_suffix}-artifacts"
    (first / "failure.png").write_bytes(b"existing evidence")
    with pytest.raises(FileExistsError, match="artifact directory is not empty"):
        prepare_ui_artifact_directory(
            "http://127.0.0.1:4321", first_evidence, RUN_ID)


def test_legacy_ui_artifacts_keep_port_scoped_directory(tmp_path, monkeypatch):
    from cli.ui_e2e import prepare_ui_artifact_directory

    monkeypatch.chdir(tmp_path)
    result = prepare_ui_artifact_directory(
        "http://127.0.0.1:4321", tmp_path / "unused.json", None)
    assert result == Path("outputs") / "ui-e2e" / "4321"


def test_stage_lineage_is_bounded_and_stays_on_selected_application(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT},
                                     "profile_id": "profile-B"},
        acceptance_run_id=RUN_ID)
    journal.record_application_readback(
        profile_id="profile-B", application_id="application-B", job_id="job-B",
        documents=[{"id": "doc-B"}])
    row = {"step": 36, "operation_id": "a" * 64, "request_id": "request-36",
           "operation_kind": "APPLY_MOCK", "application_id": "application-B",
           "profile_id": "profile-B", "job_id": "job-B"}
    journal.record_stage_lineage(row)
    assert journal.payload()["stage_lineage"] == [row]

    with pytest.raises(ValueError, match="selected B identity"):
        journal.record_stage_lineage({**row, "step": 37, "job_id": "job-A"})


def test_cover_letter_proof_is_sanitized_and_pinned_to_selected_b(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}},
        acceptance_run_id=RUN_ID)
    journal.record_application_readback(
        profile_id="profile-B", application_id="application-B", job_id="job-B",
        documents=[{"id": "document-B"}])
    digest = "a" * 64
    row = {"application_id": "application-A", "profile_id": "profile-B",
           "job_id": "job-A", "profile_version": "version-A",
           "document_id": "document-A", "version": 1,
           "sha256": {"txt": digest, "pdf": digest, "docx": digest}}
    proof = {
        "A": row,
        "B": {**row, "application_id": "application-B", "job_id": "job-B",
              "profile_version": "version-B", "document_id": "document-B"},
        "base_upload": {"revision_id": "base-revision", "sha256": digest,
                        "unchanged": True},
    }
    journal.record_cover_letter_proof(proof)
    assert journal.payload()["cover_letter_proof"]["B"]["document_id"] == "document-B"

    with pytest.raises(ValueError, match="selected B identity"):
        journal.record_cover_letter_proof({
            **proof, "B": {**proof["B"], "document_id": "document-unread"}})


def test_journal_persists_run_identity_and_acceptance_rows(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json",
        {"source": {"commit": SOURCE_COMMIT}},
        acceptance_run_id=RUN_ID,
    )
    journal.record_observation(
        step=12, status="PASS", observable_result="Jobs search narrowed the visible list.",
        evidence_kind="CONTROLLED_FIXTURE",
        checks=[{"name": "matching_job_visible", "passed": True},
                {"name": "nonmatching_job_hidden", "passed": True}],
        continuity={"profile_id": "profile-123"},
    )
    payload = journal.payload()

    assert payload["acceptance_run_id"] == RUN_ID
    assert payload["acceptance_observations"][0]["run_id"] == RUN_ID
    assert payload["acceptance_observations"][0]["source_commit"] == SOURCE_COMMIT


def test_no_hook_emits_no_acceptance_credit(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}})
    assert journal.record_observation(
        step=12, status="PASS", observable_result="Jobs search narrowed the visible list.",
        evidence_kind="CONTROLLED_FIXTURE",
        checks=[{"name": "matching_job_visible", "passed": True}],
    ) is None
    assert journal.payload()["acceptance_run_id"] is None
    assert journal.payload()["acceptance_observations"] == []
