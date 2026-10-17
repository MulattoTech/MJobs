"""Retain safe Workday structural review identity in public dispatch."""
from unittest.mock import patch
import pytest
from services.applications.form_review_providers import analyze_application_form
WORKDAY_ROUTE = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Engineer_R123"

def test_workday_preserves_structural_identity_without_question_text():
    html = ('<form><label for="email">PRIVATE_QUESTION</label>'
            '<input id="email" name="email" type="email" '
            'data-workday-conditional="false" data-workday-repeatable="false" '
            'value="PRIVATE_VALUE"></form>')
    report = analyze_application_form("workday", html, route_url=WORKDAY_ROUTE)
    assert report["status"] == "REVIEW_REQUIRED"
    assert report["dynamic_review"]["schema_version"] == 1
    assert len(report["dynamic_review"]["fingerprint"]) == 64
    assert "PRIVATE_QUESTION" not in str(report)
    assert "PRIVATE_VALUE" not in str(report)


@pytest.mark.parametrize("analysis", [
    {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []},
    {"status": "PASS", "fields": [{"id": "email", "type": "email"}]},
])
def test_invalid_workday_observation_has_no_usable_dynamic_review(analysis):
    with patch("services.applications.form_review_providers.analyze_workday_form_html",
               return_value=analysis):
        report = analyze_application_form("workday", "ignored", route_url=WORKDAY_ROUTE)
    assert report["status"] == "REVIEW_REQUIRED"
    assert "dynamic_review" not in report
