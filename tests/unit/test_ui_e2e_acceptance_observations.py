"""Safety and lineage checks for numbered UI acceptance observations."""

from pathlib import Path
import hashlib
import json
from unittest.mock import Mock

import pytest

from cli import ui_e2e
from cli.ui_e2e_jobs_journey import (
    build_observation,
    canonical_posting_count_matches,
    fixture_us_remote_explanation,
    match_sort_matches_readback,
    open_job_detail_for_selected_card,
    visible_posting_count_footer_matches,
)


RUN_ID = "01a1b028-0834-7130-b5ac-b7625e151716"
SOURCE_COMMIT = "7caf4e380bc8b5dc75024895ab400e770015a3ec"


@pytest.mark.parametrize(("detail", "saved"), [
    ({"id": "job-b", "tracking": {"disposition": "SAVED"}}, True),
    ({"id": "job-a", "tracking": {"disposition": "SAVED"}}, False),
    ({"id": "job-b", "tracking": {"disposition": "ACTIVE"}}, False),
])
def test_saved_job_readback_reloads_and_reselects_before_checking_scoped_state(monkeypatch, detail, saved):
    from unittest.mock import call

    flow = Mock()
    flow.request.return_value = detail
    monkeypatch.setattr(ui_e2e, "expect", flow.expect)
    assert ui_e2e.reload_saved_job_readback(
        flow.page, flow.reselect, flow.request, "profile-b", "job-b",
    ) is saved
    assert flow.mock_calls == [
        call.page.reload(wait_until="domcontentloaded"),
        call.page.get_by_label("Active profile", exact=True),
        call.expect(flow.page.get_by_label.return_value),
        call.expect().to_have_value("profile-b"),
        call.reselect("job-b"),
        call.page.locator("section.job-detail"),
        call.page.locator().get_by_text("Current status: SAVED", exact=True),
        call.expect(flow.page.locator.return_value.get_by_text.return_value),
        call.expect().to_be_visible(),
        call.request("/jobs/job-b?profile_id=profile-b"),
    ]


@pytest.mark.parametrize("invalid", [
    "old-revision", "other-profile", "other-job", "other-application", "wrong-profile-version",
    "duplicate-current", "wrong-application-readback", "missing-id",
])
@pytest.mark.parametrize("kind", ["resume", "cover-letter"])
def test_prepared_document_pair_rejects_unbound_or_ambiguous_readback(invalid, kind):
    application = {
        "id": "application-b", "profile_id": "profile-b", "job_id": "job-b",
        "profile_version": "reviewed", "document_version": 2,
    }
    resume = {
        "id": "resume-b", "application_id": "application-b", "profile_id": "profile-b",
        "job_id": "job-b", "profile_version": "reviewed", "kind": "resume", "version": 2,
    }
    letter = {**resume, "id": "letter-b", "kind": "cover-letter"}
    selected, other = (resume, letter) if kind == "resume" else (letter, resume)
    scope = {"application_id": "application-b", "profile_id": "profile-b", "job_id": "job-b"}
    assert ui_e2e.prepared_document_pair(
        application, [{**resume, "version": 1}, {**letter, "version": 1}, resume, letter], **scope,
    ) == {"resume": "resume-b", "cover-letter": "letter-b"}
    assert ui_e2e.prepared_document_pair(application, [other], **scope) is None
    assert ui_e2e.prepared_document_pair(
        application, [{**selected, "id": other["id"]}, other], **scope,
    ) is None
    if invalid == "wrong-application-readback":
        application["id"] = "application-a"
    changes = {
        "old-revision": {"version": 1},
        "other-profile": {"profile_id": "profile-a"},
        "other-job": {"job_id": "job-a"},
        "other-application": {"application_id": "application-a"},
        "wrong-profile-version": {"profile_version": "stale"},
        "missing-id": {"id": None},
    }
    documents = [{**selected, **changes.get(invalid, {})}, other]
    if invalid == "duplicate-current":
        documents.append({**selected, "id": "another-document"})
    assert ui_e2e.prepared_document_pair(application, documents, **scope) is None


@pytest.mark.parametrize("scenario", [
    "known-omission", "empty-diff", "wrong-pair-with-omission", "wrong-text-hash",
    "omitted-fact-still-in-tailored", "cross-application",
])
def test_generated_resume_omission_receipt_requires_actual_difference_on_the_selected_pair(scenario):
    from services.documents.comparison import compare_facts

    scope = {"application_id": "application-b", "profile_id": "profile-b", "job_id": "job-b"}
    base = {**scope, "kind": "resume", "version": 2, "id": "resume-v2",
            "profile_version": "a" * 64, "sha256": {"txt": "b" * 64}}
    tailored = {**base, "version": 4, "id": "resume-v4", "sha256": {"txt": "c" * 64}}
    python_fact = {"fact_id": "python", "kind": "skill", "text": "Python"}
    sql_fact = {"fact_id": "sql", "kind": "skill", "text": "SQL"}

    def snapshot(document, facts):
        return {"id": document["id"], "version": document["version"],
                "profile_version": document["profile_version"], "sha256": document["sha256"]["txt"],
                "facts": facts, "text": "Resume text must not be retained in the receipt."}

    base_facts = [python_fact, sql_fact]
    tailored_facts = base_facts if scenario == "empty-diff" else [python_fact]
    result = {"base": snapshot(base, base_facts), "tailored": snapshot(tailored, tailored_facts),
              **compare_facts(base_facts, tailored_facts, {})}
    if scenario == "wrong-pair-with-omission":
        result["tailored"].update(id="resume-v3", version=3)
    elif scenario == "wrong-text-hash":
        result["base"]["sha256"] = "d" * 64
    elif scenario == "omitted-fact-still-in-tailored":
        result["tailored"]["facts"].append(sql_fact)
    elif scenario == "cross-application":
        tailored["application_id"] = "application-a"

    def receipt():
        return ui_e2e.generated_resume_omission_receipt(
            result, base=base, tailored=tailored, omitted_fact=sql_fact, **scope)

    if scenario != "known-omission":
        with pytest.raises(AssertionError):
            receipt()
        return
    assert receipt() == {
        **scope,
        "documents": {
            "base": {"id": "resume-v2", "version": 2, "profile_version": "a" * 64, "sha256": "b" * 64},
            "tailored": {"id": "resume-v4", "version": 4, "profile_version": "a" * 64, "sha256": "c" * 64},
        },
        "omitted": {"fact_id": "sql", "kind": "skill", "text_sha256": hashlib.sha256(b"SQL").hexdigest()},
    }


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


def test_ui_console_summary_omits_private_fields_and_keeps_full_private_artifact(
        tmp_path, capsys):
    from tests.unit.test_acceptance_employer_verification_opt_in import (
        _controlled_profile, _controlled_ui_artifact,
    )

    profile = _controlled_profile(profile_id="PRIVATE-PROFILE-ID-SENTINEL")
    private = _controlled_ui_artifact(
        profile=profile, source={"commit": SOURCE_COMMIT, "tree": "b" * 40},
        run_id=RUN_ID, origin="http://127.0.0.1:18777")
    evidence = {
        **private,
        "result": "PARTIAL",
        "mode": "ONBOARDING_FIXTURE",
        "acceptance_observations": [{"step": 5, "status": "PASS"}],
        "confirmation": {"nested": "PRIVATE-CONFIRMATION-SENTINEL"},
        "unknown_nested": {"witness": "PRIVATE-UNKNOWN-FIELD-SENTINEL"},
        "artifacts": ["PRIVATE-UI-PATH-SENTINEL"],
    }
    evidence_path = tmp_path / "PRIVATE-UI-PATH-SENTINEL.json"
    journal = ui_e2e.EvidenceJournal(
        evidence_path, {"source": private["source"]}, acceptance_run_id=RUN_ID)
    ui_e2e.validate_employer_verification_witness(evidence, True)
    journal.complete({"action": "first"})
    journal.complete({"action": "second"})
    journal.record_observation(step=12, status="PASS", observable_result="Reviewed fixture",
        evidence_kind="CONTROLLED_FIXTURE", checks=[{"name": "matching_job_visible", "passed": True},
            {"name": "nonmatching_job_hidden", "passed": True}], continuity={"profile_id": "profile-123"})
    ui_e2e.finish_console_evidence(journal, evidence)

    rendered = capsys.readouterr().out
    for sentinel in ("PRIVATE-PROFILE-ID-SENTINEL", "PRIVATE-CONFIRMATION-SENTINEL",
                     "PRIVATE-UNKNOWN-FIELD-SENTINEL", "PRIVATE-UI-PATH-SENTINEL"):
        assert sentinel not in rendered
    summary = json.loads(rendered)
    assert summary == {
        "event": "ui_complete", "result": "PARTIAL", "mode": "ONBOARDING_FIXTURE",
        "check_count": 2, "observation_count": 1, "artifact": "ui.json",
    }
    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert saved["employer_verification_opt_in"] == private["employer_verification_opt_in"]
    assert saved["confirmation"] == evidence["confirmation"]
    assert saved["unknown_nested"] == evidence["unknown_nested"]
    assert saved["artifacts"] == evidence["artifacts"]
    assert len(saved["checks"]) == summary["check_count"]
    assert len(saved["acceptance_observations"]) == summary["observation_count"]
    assert ui_e2e.validate_employer_verification_witness(saved, True) is None


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


def test_match_sort_oracle_binds_rendered_order_to_unique_canonical_rows():
    rows = [
        {"id": "job-a", "title": "Alpha Role", "match_score": 95},
        {"id": "job-b", "title": "Beta Role", "match_score": 95},
        {"id": "job-c", "title": "Gamma Role", "match_score": 70},
    ]

    assert match_sort_matches_readback(rows, ["Alpha Role", "Beta Role", "Gamma Role"])
    assert not match_sort_matches_readback(rows, ["Beta Role", "Alpha Role", "Gamma Role"])
    assert not match_sort_matches_readback(
        [{**rows[0], "title": "Same Role"}, {**rows[1], "title": "Same Role"}],
        ["Same Role", "Same Role"],
    )
    assert not match_sort_matches_readback(
        [rows[1], rows[0], rows[2]], ["Beta Role", "Alpha Role", "Gamma Role"],
    )
    assert not match_sort_matches_readback(
        [{**rows[0], "match_score": True}, rows[1]], ["Alpha Role", "Beta Role"],
    )


def test_job_discovery_slice_passes_require_canonical_identity_for_duplicate_count():
    with pytest.raises(ValueError, match="job_id continuity"):
        build_observation(
            step=17, status="PASS",
            observable_result="Canonical count matches distinct source postings.",
            evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
            run_id=RUN_ID,
            checks=[{"name": "canonical_duplicate_job_available", "passed": True},
                    {"name": "duplicate_count_matches_readback", "passed": True}],
        )

    observation = build_observation(
        step=17, status="PASS",
        observable_result="Canonical count matches distinct source postings.",
        evidence_kind="CONTROLLED_FIXTURE", source_commit=SOURCE_COMMIT,
        run_id=RUN_ID, continuity={"profile_id": "profile-123", "job_id": "job-789"},
        checks=[{"name": "canonical_duplicate_job_available", "passed": True},
                {"name": "duplicate_count_matches_readback", "passed": True}],
    )
    assert observation["continuity"]["job_id"] == "job-789"


def test_duplicate_count_requires_distinct_source_posting_identities():
    postings = [
        {"source": "Board-A", "source_job_id": "listing-1"},
        {"source": "Board-B", "source_job_id": "listing-1"},
        {"source": "Employer", "source_job_id": "req-1"},
    ]

    assert canonical_posting_count_matches(3, postings)
    assert not canonical_posting_count_matches(2, postings)
    assert not canonical_posting_count_matches(3, postings + [postings[0]])
    assert not canonical_posting_count_matches(3, [*postings[:-1], {"source": "Employer"}])


def test_visible_duplicate_count_must_be_one_exact_item_in_selected_card_footer():
    class Item:
        def __init__(self, text, visible=True):
            self.text = text
            self.visible = visible

        def is_visible(self):
            return self.visible

        def inner_text(self):
            return self.text

    class Items:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Footer:
        def __init__(self, items, visible=True):
            self.items = items
            self.visible = visible

        def count(self):
            return 1

        def is_visible(self):
            return self.visible

        def locator(self, selector):
            assert selector == ":scope > span"
            return Items(self.items)

    class MissingFooter:
        def count(self):
            return 0

    class Card:
        def __init__(self, footer, card_text="Top badge says 2 postings, footer says one source"):
            self.footer = footer
            self.card_text = card_text

        def locator(self, selector):
            assert selector == ".card-foot"
            return self.footer

        def inner_text(self):
            return self.card_text

    # The whole card can contain the expected wording outside its footer; it earns no credit.
    wrong_footer = Card(Footer([Item("1 posting")]))
    assert "2 postings" in wrong_footer.inner_text()
    assert visible_posting_count_footer_matches(wrong_footer, 2) is False
    assert visible_posting_count_footer_matches(
        Card(Footer([Item("2 postings"), Item("2 postings")])), 2) is False
    assert visible_posting_count_footer_matches(Card(MissingFooter()), 2) is False
    assert visible_posting_count_footer_matches(Card(Footer([Item("2 postings")])), 2)
    assert not visible_posting_count_footer_matches(
        Card(Footer([Item("2 postings", visible=False)])), 2)


def test_remote_card_selection_creates_detail_region_before_opening_and_reading_it():
    events = []

    class DetailRegion:
        def get_by_role(self, role, **kwargs):
            events.append(("get_by_role", role, kwargs))
            return "remote-heading"

    class Page:
        def locator(self, selector):
            events.append(("locator", selector))
            return detail_region

    class Card:
        def click(self):
            events.append(("click",))

    class VisibilityAssertion:
        def to_be_visible(self):
            events.append(("visible",))

    def expect(locator):
        events.append(("expect", locator))
        return VisibilityAssertion()

    detail_region = DetailRegion()
    result = open_job_detail_for_selected_card(
        Page(), Card(), "Remote Systems Administrator", expect)

    assert result is detail_region
    assert [event[0] for event in events] == [
        "locator", "click", "get_by_role", "expect", "visible",
    ]


def test_remote_match_explanation_shows_recorded_job_regions_not_profile_location():
    from services.discovery.matching import build_explanation

    result = build_explanation(
        {"skills": ["Python"], "remote_type": "REMOTE_SPECIFIC_STATES",
         "attributes": {"skill_evidence": "description_mentions",
                        "remote_regions": ["OK", "TX", "TX", "\n"]}},
        {"facts": [], "contact": {"country": "United States", "state": "Oklahoma"}},
    )
    factor = next(row for row in result["components"]["fit_factors"]
                  if row["id"] == "remote_location")

    assert "Recorded job regions: OK, TX." in factor["explanation"]
    assert "Oklahoma" not in factor["explanation"]
    assert factor["score_effect"] == "NONE"


@pytest.mark.parametrize("readback_overrides", [
    {},
    {"profile_id": "other-profile"},
    {"id": "existing-discovery-search"},
    {"criteria": {"remote": "REQUIRED", "remote_regions": []}},
])
def test_step16_creates_and_reads_a_fresh_schedule_off_search(readback_overrides):
    from types import SimpleNamespace

    from apps.api.main import SearchProfileInput
    from packages.domain.automation import SavedSearchRule

    writes = []

    def put(path, payload):
        assert path == "/search-profiles"
        parsed = SearchProfileInput.model_validate(payload)
        assert SavedSearchRule.from_mapping(parsed.scheduler).enabled is False
        writes.append(payload)
        return SimpleNamespace(ok=True, json=lambda: {"id": payload["id"]})

    def read(path):
        payload = writes[-1]
        assert path == f"/search-profiles/{payload['id']}"
        assert payload["id"].startswith("fixture-step16-")
        assert payload["profile_id"] == "fixture-profile"
        assert payload["enabled"] is False
        assert payload["scheduler"] == {"enabled": False, "interval_seconds": 1800}
        assert payload["remote"] == "REQUIRED"
        assert payload["remote_regions"] == ["US"]
        assert "include_titles" not in payload
        return {**payload, "source_ids": [], "criteria": {
            "remote": payload["remote"], "remote_regions": payload["remote_regions"],
            "include_titles": [], "scheduler": payload["scheduler"],
        }, **readback_overrides}

    if readback_overrides:
        with pytest.raises(AssertionError, match="search readback"):
            ui_e2e.create_fixture_us_remote_search(put, read, "fixture-profile")
    else:
        first = ui_e2e.create_fixture_us_remote_search(put, read, "fixture-profile")
        second = ui_e2e.create_fixture_us_remote_search(put, read, "fixture-profile")
        assert first == writes[0]["id"] and second == writes[1]["id"] and first != second


@pytest.mark.parametrize("job_regions, detail_regions, detail_id, explanation, expected", [
    (["US"], ["US"], "same-job", "Recorded job regions: US.", True),
    ([], ["US"], "same-job", "Recorded job regions: US.", False),
    (["US"], [], "same-job", "Recorded job regions: US.", False),
    (["US"], ["CA"], "same-job", "Recorded job regions: US.", False),
    (["US"], ["US"], "other-job", "Recorded job regions: US.", False),
    (["US"], ["US"], "same-job", "The selected profile is in the US.", False),
    (["US"], ["US"], "same-job", "Recorded job regions: CA.", False),
    (["US"], ["US"], "same-job", None, False),
])
def test_step16_explanation_binds_same_canonical_job_and_recorded_regions(
        job_regions, detail_regions, detail_id, explanation, expected):
    job = {"id": "same-job", "title": "Fixture role", "remote_type": "REMOTE_US",
           "attributes": {"fictional": True, "remote_regions": job_regions}}
    detail = {**job, "id": detail_id,
              "attributes": {"fictional": True, "remote_regions": detail_regions},
              "match": {"components": {"fit_factors": [
                  {"id": "remote_location", "label": "Remote work region", "explanation": explanation},
              ]}}}
    assert fixture_us_remote_explanation(job, detail) == (explanation if expected else None)


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
        documents=[{"id": "resume-B"}, {"id": "letter-B"}])
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
        stage_lineage=lineage, document_ids=["resume-B", "letter-B"])

    assert result["status"] == "PASS"
    assert journal.payload()["stage_lineage"] == [lineage]
    assert journal.payload()["acceptance_observations"][0]["continuity"] == {
        "profile_id": "profile-B", "application_id": "app-B", "job_id": "job-B",
        "document_ids": ["resume-B", "letter-B"],
    }


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


@pytest.mark.parametrize("provider, label", [
    ("mock-ats", "Fictional mock ATS"),
    ("greenhouse", "Greenhouse"),
    ("lever", "Lever"),
    ("unknown-provider", None),
    (None, None),
])
def test_application_provider_label_uses_only_explicit_known_mapping(provider, label):
    from cli.ui_e2e import application_provider_label

    assert application_provider_label(provider) == label


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


@pytest.mark.parametrize("count, disabled, expected", [
    (0, False, True),
    (1, True, True),
    (1, False, False),
    (2, True, False),
])
def test_submit_control_is_absent_or_a_single_disabled_control(count, disabled, expected):
    from cli.ui_e2e import submit_control_is_absent_or_disabled

    class Locator:
        def count(self):
            return count

        def is_disabled(self):
            return disabled

    assert submit_control_is_absent_or_disabled(Locator()) is expected


def test_current_preflight_question_is_selected_by_unique_question_identity():
    from cli.ui_e2e import current_missing_question_by_id

    country = {"question_id": "country", "question": "Country", "answer_ids": ["country"]}
    fixture = {"question_id": "fixture_question",
               "question": "Approved fixture answer",
               "answer_ids": ["fixture_question"]}
    preflight = {"is_current": True, "missing": [country, fixture]}

    assert current_missing_question_by_id(preflight, "fixture_question") is fixture

    with pytest.raises(AssertionError):
        current_missing_question_by_id(
            {"is_current": False, "missing": [fixture]}, "fixture_question")
    with pytest.raises(AssertionError):
        current_missing_question_by_id(
            {"is_current": True, "missing": [fixture, dict(fixture)]},
            "fixture_question")


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
