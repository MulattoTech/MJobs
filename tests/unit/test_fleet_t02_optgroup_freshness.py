"""Grouped select labels must invalidate value-free Workday snapshots."""
import hashlib
import json

import pytest

from services.applications.adapters.workday_forms import (
    analyze_workday_form_html,
    validate_workday_form_html,
)
from services.applications.workday_dynamic_review import project_workday_dynamic_review


@pytest.mark.parametrize("options", [
    '<optgroup label="Region"><option>US</option><option>Canada</option></optgroup>',
    '<optgroup label="Region"><option>US<option>Canada</optgroup>',
    '<optgroup label="Region"><option>US<option>Canada',
    '<option>US<option>Canada',
    '<optgroup label="Region"><option>US<optgroup label="Other"><option>Canada',
    '<optgroup label="Region"><option label="US">ignored</option></optgroup>'
    '<optgroup label="Other"><option>Canada</option></optgroup>',
])
def test_grouped_choice_change_invalidates_snapshot_and_durable_fingerprint(options):
    source = '<form><select id="country" aria-label="Country">' + options + '</select></form>'
    old = analyze_workday_form_html(source)
    assert old["status"] == "REVIEW_REQUIRED"
    changed = source.replace("US", "Mexico")
    assert validate_workday_form_html(changed, old)["reason"] == "FORM_CHANGED"
    assert old["fields"][0]["options_fingerprint"] == hashlib.sha256(
        json.dumps(["US", "Canada"], separators=(",", ":")).encode()
    ).hexdigest()
    before = project_workday_dynamic_review(old)
    after = project_workday_dynamic_review(analyze_workday_form_html(changed))
    assert before is not None and after is not None
    assert before["fingerprint"] != after["fingerprint"]
    assert "Mexico" not in json.dumps(after)


def test_grouped_option_answers_and_inert_content_do_not_enter_projection():
    source = ('<form><select id="country" aria-label="Country"><optgroup label="Region">'
              '<option value="PRIVATE_A" selected>US</option>'
              '<template><option>PRIVATE_INERT</option></template></optgroup></select>'
              '<template><option>PRIVATE_TEMPLATE</option></template></form>')
    old = analyze_workday_form_html(source)
    assert old["status"] == "REVIEW_REQUIRED"
    assert old["fields"][0]["options_fingerprint"] == hashlib.sha256(b'["US"]').hexdigest()
    assert old == analyze_workday_form_html(source.replace("PRIVATE_A", "PRIVATE_B").replace(" selected", ""))
    assert "PRIVATE" not in json.dumps(old)


@pytest.mark.parametrize("ending", ["</form>", "</optgroup></form>"])
def test_missing_select_close_fails_closed(ending):
    source = '<form><select id="country"><optgroup><option>US' + ending
    assert analyze_workday_form_html(source)["status"] == "UNKNOWN"


@pytest.mark.parametrize("options", [
    '<option>' + 'x' * 501 + '</option>',
    '<option label="' + 'x' * 501 + '">US</option>',
    '<option>US</option>' * 101,
])
def test_grouped_option_limits_fail_closed(options):
    source = '<form><select id="country"><optgroup>' + options + '</optgroup></select></form>'
    assert analyze_workday_form_html(source)["status"] == "UNKNOWN"
