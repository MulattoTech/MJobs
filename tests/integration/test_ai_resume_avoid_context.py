"""Avoid restrictions apply before a selected AI resume context reaches a provider."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from services.documents.errors import PreparationError
from tests.integration import test_ai_resume_fact_trace as fixture_support


@pytest.fixture
def scenario():
    # Reuse the real SQLite/capturing-provider fixture without inheriting its tests.
    case = fixture_support.AIResumeTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def save_preferences(case, terms, *, source=None):
    profile = deepcopy(case.profile_data)
    profile["preferences"] = {**profile.get("preferences", {}), "avoid_terms": terms}
    if source is not None:
        next(fact for fact in profile["facts"] if fact["id"] == "experience")["text"] = source
    case.store.save_profile(profile)


def assert_blocked_before_provider(case):
    factory = Mock(return_value=case.provider)
    profile_before = case.store.profile("fixture")
    job_before = case.store.job("ai-resume-job", "fixture")
    with case.store.engine.connect() as connection:
        audit_before = list(connection.execute(select(m.audit)).mappings())
    files_before = {str(path.relative_to(case.output)): path.read_bytes()
                    for path in case.output.rglob("*") if path.is_file()}

    blocked_error = None
    try:
        case.request(provider_factory=factory)
    except PreparationError as error:
        blocked_error = error

    # Assert the admission boundary even if later proposal validation also fails.
    factory.assert_not_called()
    assert case.provider.calls == []
    assert blocked_error is not None
    assert blocked_error.code == "AVOID_LIST_BLOCKED"
    assert "AI resume" in str(blocked_error)
    assert "Unselected confidential source canary" not in str(blocked_error)
    assert "private-contact-canary" not in str(blocked_error)
    assert case.store.profile("fixture") == profile_before
    assert case.store.job("ai-resume-job", "fixture") == job_before
    with case.store.engine.connect() as connection:
        assert list(connection.execute(select(m.audit)).mappings()) == audit_before
    assert {str(path.relative_to(case.output)): path.read_bytes()
            for path in case.output.rglob("*") if path.is_file()} == files_before
    case.assert_no_documents()


def test_selected_approved_fact_is_refused_before_any_provider_or_request_receipt(scenario):
    save_preferences(scenario, ["Linux"])
    assert_blocked_before_provider(scenario)


@pytest.mark.parametrize("term,source_term", [
    ("North Star", "North\nStar"),
    ('ACME "Research"', 'ACME "Research"'),
    ("North\\Star", "North\\Star"),
    ("Café", "Cafe\u0301"),
    ("AI", "ＡＩ"),
])
def test_raw_fact_values_are_checked_before_json_escaping(scenario, term, source_term):
    source = f"Managed 40 Linux servers for {source_term} in 2020-2022."
    save_preferences(scenario, [term], source=source)
    scenario.provider.value = {"rewrites": [{
        "fact_id": "experience", "text": source.replace("Managed", "Maintained"),
    }]}
    assert_blocked_before_provider(scenario)


@pytest.mark.parametrize("field", ["title", "description"])
def test_projected_target_job_content_is_checked(scenario, field):
    save_preferences(scenario, ["North Star"])
    with scenario.store.engine.begin() as connection:
        connection.execute(update(m.jobs).where(m.jobs.c.id == "ai-resume-job").values(
            **{field: "North\nStar's systems engineering opportunity"},
        ))
    assert_blocked_before_provider(scenario)


@pytest.mark.parametrize("term", ["experience", "responsibility"])
def test_selected_fact_identifier_and_kind_are_also_dynamic_context(scenario, term):
    save_preferences(scenario, [term])
    assert_blocked_before_provider(scenario)


def test_unselected_private_fields_and_text_beyond_projection_are_not_scanned(scenario):
    save_preferences(scenario, [
        "Unselected confidential source canary", "private-contact-canary",
        "synthetic-approved-record", "omitted-target",
    ])
    with scenario.store.engine.begin() as connection:
        connection.execute(update(m.jobs).where(m.jobs.c.id == "ai-resume-job").values(
            title="T" * 512 + " omitted-target",
            description="D" * 8_000 + " omitted-target",
        ))

    result = scenario.request()

    assert result["state"] == "REVIEW_REQUIRED"
    assert len(scenario.provider.calls) == 1
    data = json.loads(scenario.provider.calls[0]["prompt"].split("\n", 1)[1])
    assert data == {
        "approved_facts": [{"fact_id": "experience", "kind": "responsibility",
                            "text": fixture_support.SOURCE}],
        "target_job": {"title": "T" * 512, "description": "D" * 8_000},
    }
    scenario.assert_no_documents()


def test_independent_fields_do_not_create_a_prohibited_phrase(scenario):
    source = fixture_support.SOURCE + " North"
    save_preferences(scenario, ["North Star"], source=source)
    scenario.provider.value = {"rewrites": [{
        "fact_id": "experience", "text": fixture_support.PROPOSAL + " North",
    }]}
    with scenario.store.engine.begin() as connection:
        connection.execute(update(m.jobs).where(m.jobs.c.id == "ai-resume-job").values(
            title="Star systems engineer",
        ))

    result = scenario.request()

    assert result["state"] == "REVIEW_REQUIRED"
    assert len(scenario.provider.calls) == 1
    data = json.loads(scenario.provider.calls[0]["prompt"].split("\n", 1)[1])
    assert data["approved_facts"][0]["text"] == source
    assert data["target_job"]["title"] == "Star systems engineer"


def test_provider_introduced_term_remains_blocked_by_final_output_validation(scenario):
    # The source says Managed; the fixture proposes Maintained after admission.
    save_preferences(scenario, ["Maintained"])
    draft = scenario.request()
    assert draft["state"] == "REVIEW_REQUIRED"
    assert len(scenario.provider.calls) == 1

    with pytest.raises(PreparationError) as raised:
        scenario.accept(draft)

    assert raised.value.code == "AVOID_LIST_BLOCKED"
    assert scenario.store.count(m.documents.name) == 0
