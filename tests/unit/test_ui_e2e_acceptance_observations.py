"""Safety and lineage checks for numbered UI acceptance observations."""

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


def test_evidence_journal_records_actual_readback_lineage(tmp_path):
    from cli.ui_e2e import EvidenceJournal

    journal = EvidenceJournal(
        tmp_path / "evidence.json", {"source": {"commit": SOURCE_COMMIT}}, acceptance_run_id=RUN_ID)
    journal.record_observed_jobs(["job-A"])
    journal.record_application_readback(
        application_id="app-B", job_id="job-B",
        documents=[{"id": "doc-1"}, {"id": "doc-2"}])
    payload = journal.payload()
    assert payload["observed_job_ids"] == ["job-A"]
    assert payload["selected_application_job_id"] == "job-B"
    assert payload["selected_application_id"] == "app-B"
    assert payload["application_document_ids"] == ["doc-1", "doc-2"]


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
