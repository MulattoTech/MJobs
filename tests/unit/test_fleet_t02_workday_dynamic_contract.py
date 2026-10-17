"""Strict, value-free Workday review envelope boundary."""
from copy import deepcopy
import json

import pytest

from services.applications.adapters.workday_forms import analyze_workday_form_html


MARKUP = """<form><label for="country">Country</label>
<select id="country" name="country" data-workday-conditional="false"
 data-workday-repeatable="false"><option selected>PRIVATE_CHOICE</option></select>
<label for="region">Region</label><input id="region" name="region"
 data-workday-conditional="true" data-workday-conditional-on="country"
 data-workday-repeatable="false" value="PRIVATE_ANSWER"></form>"""


def project(markup=MARKUP):
    from services.applications.workday_dynamic_review import project_workday_dynamic_review
    return project_workday_dynamic_review(analyze_workday_form_html(markup))


def test_structure_survives_without_labels_or_applicant_values():
    envelope = project()
    assert envelope["schema_version"] == 1
    assert envelope["fields"][1]["conditional_on"] == "country"
    serialized = json.dumps(envelope)
    for secret in ("PRIVATE_CHOICE", "PRIVATE_ANSWER", "Country", "Region"):
        assert secret not in serialized


def test_question_changes_invalidate_identity_but_answer_changes_do_not():
    original = project()["fingerprint"]
    assert project(MARKUP.replace("PRIVATE_ANSWER", "OTHER_PRIVATE_ANSWER"))["fingerprint"] == original
    assert project(MARKUP.replace(">Region<", ">Province<"))["fingerprint"] != original
    assert project(MARKUP.replace('id="region"', 'required id="region"'))["fingerprint"] != original


@pytest.mark.parametrize("change", ["value", "hash", "duplicate", "dependency", "boolean"])
def test_forged_durable_envelope_is_rejected(change):
    from services.applications.workday_dynamic_review import validate_workday_dynamic_review
    envelope = deepcopy(project())
    if change == "value":
        envelope["fields"][0]["value"] = "PRIVATE"
    elif change == "hash":
        envelope["fingerprint"] = "0" * 64
    elif change == "duplicate":
        envelope["fields"].append(deepcopy(envelope["fields"][0]))
    elif change == "dependency":
        envelope["fields"][1]["conditional_on"] = "missing"
    else:
        envelope["fields"][1]["conditional"] = 1
    assert validate_workday_dynamic_review(envelope) is None


def test_validator_returns_detached_validated_data():
    from services.applications.workday_dynamic_review import validate_workday_dynamic_review
    source = project()
    validated = validate_workday_dynamic_review(source)
    assert validated == source
    source["fields"][0]["id"] = "mutated"
    assert validated["fields"][0]["id"] == "country"


@pytest.mark.parametrize("key,value", [
    ("type", []), ("required", {}), ("conditional_on", set()),
    ("sensitivity", []), ("id", {}), ("repeatable", []),
])
def test_malformed_analyzer_scalars_fail_closed(key, value):
    from services.applications.workday_dynamic_review import project_workday_dynamic_review
    analysis = analyze_workday_form_html(MARKUP)
    analysis["fields"][0][key] = value
    assert project_workday_dynamic_review(analysis) is None
