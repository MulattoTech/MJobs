"""Owned fixture admission binds reviewed inputs before browser interaction."""
import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from services.applications.form_analysis import analyze_controls
from services.applications.upload_integrity import snapshot_bytes


def fixture_inputs(provider="greenhouse", *, custom=False):
    controls = [
        {"tag": "input", "name": "email", "input_type": "email"},
        {"tag": "input", "name": "resume", "input_type": "file"},
        {"tag": "input", "name": "cover_letter", "input_type": "file"},
    ]
    if custom:
        controls.append({"tag": "textarea", "name": "availability",
                         "aria_label": "Which days are you available?"})
    documents = []
    for kind in ("resume", "cover-letter"):
        content = b"%PDF-1.4\nsynthetic " + kind.encode()
        documents.append(snapshot_bytes(kind, kind + "-1", kind + ".pdf",
                                        "application/pdf", content,
                                        hashlib.sha256(content).hexdigest()))
    route = ("https://boards.greenhouse.io/example/jobs/12345" if provider == "greenhouse"
             else "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef")
    return {"workspace_mode": "FIXTURE", "application_id": "application-1", "provider": provider,
                "route": {"provider": provider, "url": route}, "origin": "http://127.0.0.1:8765",
                "page_url": "http://127.0.0.1:8765/owned/application", "template": b"<form>owned fixture</form>",
                "analysis": analyze_controls(controls), "profile": {"id": "profile-1", "version": 3,
                    "email": "synthetic@example.invalid", "answers": [{"id": "availability-1",
                        "question": "Which days are you available?", "text": "Weekdays only.",
                        "verified": True, "user_approved": True}]}, "document_version": 4,
                "documents": documents,
                "reviewed_fields": ({"field_key": "availability", "answer_id": "availability-1"},)
                if custom else ()}


@pytest.mark.parametrize("provider", ["greenhouse", "lever"])
def test_contract_is_immutable_and_contains_no_applicant_values(provider):
    from services.applications.owned_ats_fixture import freeze_fixture, validate_fixture
    inputs = fixture_inputs(provider, custom=True)
    contract = freeze_fixture(**inputs)
    assert validate_fixture(contract, **inputs) == contract
    assert contract.provider == provider
    assert contract.reviewed_fields[0].field_key == "availability"
    assert "Weekdays only." not in repr(contract)
    assert "synthetic@example.invalid" not in repr(contract)
    assert "%PDF" not in repr(contract)
    with pytest.raises(FrozenInstanceError):
        contract.provider = "lever"


@pytest.mark.parametrize("change", ["profile_version", "profile_value", "document_version",
                                   "document_bytes", "document_id", "question", "template",
                                   "origin", "page_url", "provider", "route", "application"])
def test_changed_reviewed_input_is_rejected(change):
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
        validate_fixture,
    )
    original = fixture_inputs(custom=True)
    contract = freeze_fixture(**original)
    current = deepcopy(original)
    if change == "profile_version":
        current["profile"]["version"] += 1
    elif change == "profile_value":
        current["profile"]["answers"][0]["text"] = "A changed answer."
    elif change == "document_version":
        current["document_version"] += 1
    elif change == "document_bytes":
        document = current["documents"][0]
        content = document.content + b"changed"
        current["documents"][0] = replace(document, content=content, sha256=hashlib.sha256(content).hexdigest())
    elif change == "document_id":
        current["documents"][0] = replace(current["documents"][0], document_id="other-document")
    elif change == "question":
        current["analysis"]["fields"][-1]["aria_label"] = "Which nights are you available?"
        current["profile"]["answers"][0]["question"] = "Which nights are you available?"
    elif change == "template":
        current["template"] += b"changed"
    elif change == "origin":
        current["origin"] = "http://127.0.0.1:8766"
        current["page_url"] = "http://127.0.0.1:8766/owned/application"
    elif change == "page_url":
        current["page_url"] += "/other"
    elif change == "provider":
        current.update(provider="lever", route=fixture_inputs("lever")["route"])
    elif change == "route":
        current["route"]["url"] = "https://boards.greenhouse.io/example/jobs/54321"
    else:
        current["application_id"] = "application-2"
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        validate_fixture(contract, **current)


@pytest.mark.parametrize("origin", ["https://127.0.0.1:8765", "http://example.com:8765",
    "http://127.0.0.1.evil.test:8765", "http://user@127.0.0.1:8765", "http://127.0.0.1:8765/path",
    "http://127.0.0.1:8765?query=1", "http://127.0.0.1:8765#fragment", "http://localhost:8765",
    "http://127.0.0.1", "http://127.0.0.1:0", " http://127.0.0.1:8765"])
def test_only_explicit_numeric_loopback_origin_is_admitted(origin):
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
    )
    inputs = fixture_inputs()
    inputs.update(origin=origin, page_url=origin + "/owned/application")
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ORIGIN_INVALID"):
        freeze_fixture(**inputs)


@pytest.mark.parametrize("change", ["missing_review", "unapproved", "unverified", "wrong_question",
                                   "duplicate_review", "unknown_answer", "sensitive", "ambiguous"])
def test_unknown_field_requires_exact_safe_approved_answer(change):
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
    )
    inputs = fixture_inputs("lever", custom=True)
    if change == "missing_review":
        inputs["reviewed_fields"] = ()
    elif change == "unapproved":
        inputs["profile"]["answers"][0]["user_approved"] = False
    elif change == "unverified":
        inputs["profile"]["answers"][0]["verified"] = False
    elif change == "wrong_question":
        inputs["profile"]["answers"][0]["question"] = "Another question"
    elif change == "duplicate_review":
        inputs["reviewed_fields"] *= 2
    elif change == "unknown_answer":
        inputs["reviewed_fields"] = ({"field_key": "availability", "answer_id": "missing"},)
    elif change == "sensitive":
        inputs["analysis"]["fields"][-1]["sensitivity"] = "SENSITIVE"
    else:
        inputs["analysis"]["fields"][-1]["ambiguous"] = True
    with pytest.raises(OwnedATSFixtureError):
        freeze_fixture(**inputs)


@pytest.mark.parametrize("change", ["mode", "loopback_route", "provider", "corrupt_document",
                                   "missing_document", "duplicate_document", "boolean_version",
                                   "analysis_value", "empty_analysis"])
def test_invalid_input_cannot_create_review_contract(change):
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
    )
    inputs = fixture_inputs()
    if change == "mode":
        inputs["workspace_mode"] = "PERSONAL"
    elif change == "loopback_route":
        inputs["route"]["url"] = inputs["page_url"]
    elif change == "provider":
        inputs["provider"] = "mock-ats"
    elif change == "corrupt_document":
        inputs["documents"][0] = replace(inputs["documents"][0], content=b"%PDF-corrupt")
    elif change == "missing_document":
        inputs["documents"].pop()
    elif change == "duplicate_document":
        inputs["documents"][1] = inputs["documents"][0]
    elif change == "boolean_version":
        inputs["profile"]["version"] = True
    elif change == "analysis_value":
        inputs["analysis"]["fields"][0]["value"] = "PRIVATE"
    else:
        inputs["analysis"]["fields"] = []
    with pytest.raises(OwnedATSFixtureError):
        freeze_fixture(**inputs)


def test_stale_contract_stops_before_reading_browser():
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
        upload_fixture_documents,
    )
    inputs = fixture_inputs()
    contract = freeze_fixture(**inputs)
    inputs["profile"]["version"] += 1

    class UnavailablePage:
        def __getattr__(self, name):
            raise AssertionError("Stale review reached the browser")

    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        upload_fixture_documents(UnavailablePage(), contract, current=inputs)


def test_json_persisted_contract_validates_without_reapproving_inputs():
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
        validate_fixture,
    )
    inputs = fixture_inputs(custom=True)
    contract = freeze_fixture(**inputs)
    stored = json.loads(json.dumps(asdict(contract)))
    assert validate_fixture(stored, **inputs) == contract
    stored["reviewed_fields"][0]["answer_sha256"] = "0" * 64
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        validate_fixture(stored, **inputs)


def test_boolean_cannot_impersonate_persisted_numeric_version():
    from services.applications.owned_ats_fixture import (
        OwnedATSFixtureError,
        freeze_fixture,
        validate_fixture,
    )
    inputs = fixture_inputs()
    inputs["profile"]["version"] = 1
    forged = replace(freeze_fixture(**inputs), profile_version=True)
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        validate_fixture(forged, **inputs)


def test_durable_contract_hashes_private_document_filename():
    from services.applications.owned_ats_fixture import freeze_fixture
    inputs = fixture_inputs()
    inputs["documents"][0] = replace(inputs["documents"][0], filename="PRIVATE_PERSON_RESUME.pdf")
    contract = freeze_fixture(**inputs)
    assert "PRIVATE_PERSON" not in json.dumps(asdict(contract))


def test_browser_consumer_propagates_cancellation_before_dom_read():
    from services.applications.owned_ats_fixture import (
        freeze_fixture,
        upload_fixture_documents,
    )
    from workers.operations import OperationCancelled
    inputs = fixture_inputs()
    contract = freeze_fixture(**inputs)

    def cancelled():
        raise OperationCancelled("cancelled")

    with pytest.raises(OperationCancelled):
        upload_fixture_documents(object(), contract, current=inputs, check=cancelled)
