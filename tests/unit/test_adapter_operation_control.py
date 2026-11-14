"""Read-only DOM probes must preserve durable operation-control failures."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from services.applications.adapters.breezy import BreezyApplicationAdapter
from services.applications.adapters.smartrecruiters import SmartRecruitersApplicationAdapter
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter
from services.applications.adapters.workable import WorkableApplicationAdapter
from services.applications.form_analysis import analyze_controls, analyze_form_page
from workers.operations import (
    OperationCancelled, OperationControlUnavailable, OperationTimedOut,
)


class ProbePage:
    """Synthetic DOM facade with one explicitly selected failing read."""

    url = "https://jobs.smartrecruiters.com/example/12345678-engineer"

    def __init__(self, failure, error):
        self.failure = failure
        self.error = error
        self.reads = []
        self.form_evaluation_stage = "form_evaluate"

    def read(self, stage):
        self.reads.append(stage)
        if stage == self.failure:
            raise self.error

    def locator(self, selector):
        if selector == "form":
            self.read("form_locator")
        return ProbeLocator(self, selector)


class ProbeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def locator(self, selector):
        return ProbeLocator(self.page, selector)

    def count(self):
        if self.selector == "form":
            self.page.read("form_count")
            return 1
        self.page.read("challenge_count")
        return 0

    def evaluate(self, expression, arg=None, *, timeout=None):
        self.page.read(self.page.form_evaluation_stage)
        return []

    def evaluate_all(self, expression, arg=None):
        self.page.read("control_states")
        return []


CASES = (
    ("shared_locator", "shared", "form_locator", "UNKNOWN", "DOM_ANALYSIS_FAILED"),
    ("shared_count", "shared", "form_count", "UNKNOWN", "DOM_ANALYSIS_FAILED"),
    ("shared_metadata", "shared", "form_evaluate", "UNKNOWN", "DOM_ANALYSIS_FAILED"),
    ("breezy_states", "breezy", "control_states", "PASS", None),
    ("breezy_labels", "breezy", "form_evaluate", "UNKNOWN", "DOM_ANALYSIS_FAILED"),
    ("smartrecruiters_challenge", "smartrecruiters", "challenge_count",
     "UNKNOWN", "CHALLENGE_CHECK_UNAVAILABLE"),
    ("teamtailor_challenge", "teamtailor", "challenge_count",
     "UNKNOWN", "CHALLENGE_DETECTION_UNAVAILABLE"),
    ("teamtailor_states", "teamtailor", "control_states",
     "UNKNOWN", "CONTROL_STATE_UNAVAILABLE"),
    ("workable_challenge", "workable", "challenge_count",
     "UNKNOWN", "CHALLENGE_CHECK_UNAVAILABLE"),
    ("workable_states", "workable", "control_states",
     "UNKNOWN", "CONTROL_INTERACTIVITY_UNAVAILABLE"),
)
ADAPTERS = {
    "breezy": BreezyApplicationAdapter,
    "smartrecruiters": SmartRecruitersApplicationAdapter,
    "teamtailor": TeamtailorApplicationAdapter,
    "workable": WorkableApplicationAdapter,
}
ERROR_TYPES = (OperationCancelled, OperationTimedOut, OperationControlUnavailable)


class BudgetCheck:
    def __call__(self):
        pass

    def remaining_seconds(self):
        return 3.5


def operation_check(mode):
    if mode == "budget":
        return BudgetCheck()
    return (lambda: None) if mode else None


def invoke(case, page, check):
    name, provider, *_ = case
    if provider == "shared":
        return analyze_form_page(page, check=check)
    if name in {"breezy_states", "teamtailor_states"}:
        page.form_evaluation_stage = "control_states"
    analysis = analyze_controls([{
        "tag": "input", "input_type": "email", "label": "Email",
        "name": "email", "id": "email",
    }])
    assert analysis["status"] == "PASS"
    if name == "breezy_labels":
        analysis = {"status": "UNKNOWN", "reason": "UNSAFE_TEXT_CONTEXT", "fields": []}
    with patch(
        f"services.applications.adapters.{provider}.analyze_form_page",
        return_value=deepcopy(analysis),
    ):
        return ADAPTERS[provider]().analyze_page(page, check=check)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
@pytest.mark.parametrize("error_type", ERROR_TYPES, ids=lambda kind: kind.__name__)
@pytest.mark.parametrize("with_check", (False, True, "budget"),
                         ids=("no_callback", "callback", "budget_callback"))
def test_dom_probe_preserves_control_exception_identity(case, error_type, with_check):
    error = error_type()
    page = ProbePage(case[2], error)
    check = operation_check(with_check)

    with pytest.raises(error_type) as caught:
        invoke(case, page, check)

    assert caught.value is error
    assert page.reads[-1] == case[2]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
@pytest.mark.parametrize("with_check", (False, True, "budget"),
                         ids=("no_callback", "callback", "budget_callback"))
def test_ordinary_dom_failure_keeps_existing_value_free_fallback(case, with_check):
    page = ProbePage(case[2], RuntimeError("SYNTHETIC_PRIVATE_FORM_TEXT"))
    result = invoke(case, page, operation_check(with_check))

    assert page.reads[-1] == case[2]
    assert result["status"] == case[3]
    assert result.get("reason") == case[4]
    assert "SYNTHETIC_PRIVATE_FORM_TEXT" not in repr(result)
    if case[0] == "breezy_states":
        assert result["fields"]
        assert all("user_visible" not in field and "user_editable" not in field
                   for field in result["fields"])
    else:
        assert result["fields"] == []


@pytest.mark.parametrize("error_type", ERROR_TYPES, ids=lambda kind: kind.__name__)
def test_shared_analyzer_does_not_recheck_away_a_control_failure(error_type):
    error = error_type()
    checks = []

    def check():
        checks.append(True)
        if len(checks) == 2:
            raise error

    page = ProbePage("never", RuntimeError("unused"))
    with pytest.raises(error_type) as caught:
        analyze_form_page(page, check=check)

    assert caught.value is error
    assert len(checks) == 2
    assert page.reads == ["form_locator", "form_count"]
