"""Public MOCK run admission and native adapters against an owned HTTP fixture."""
import json
import os
from copy import deepcopy
from unittest.mock import patch

import httpx
import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import create_mock_app, fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_answer_bindings import MOCK_ANSWER_SLOTS
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


@pytest.mark.parametrize("provider", ["greenhouse", "lever"])
def test_native_selection_requires_exact_owned_mock_route(provider):
    from services.applications.owned_ats_execution import native_route
    origin = "http://127.0.0.1:8090"
    route = {"provider": "mock-ats", "tenant": "example", "requisition": "B",
             "url": f"{origin}/ats/native/{provider}/B"}
    assert native_route(route, origin)["provider"] == provider
    for changes in ({"provider": provider}, {"tenant": "other"}, {"requisition": "A"},
                    {"url": route["url"] + "?submit=1"},
                    {"url": route["url"].replace("127.0.0.1", "example.invalid")}):
        with pytest.raises(ValueError, match="NATIVE_ROUTE_INVALID"):
            native_route({**route, **changes}, origin)
    assert native_route({**route, "url": origin + "/ats/apply/B"}, origin) is None


def test_native_route_never_selects_a_public_provider_url():
    from services.applications.owned_ats_execution import native_route
    assert native_route({"provider": "greenhouse", "url":
                         "https://boards.greenhouse.io/example/jobs/123"},
                        "http://127.0.0.1:8090") is None


@pytest.fixture(params=["greenhouse", "lever"])
def native_application(tmp_path, request):
    store = Store(f"sqlite:///{(tmp_path / 'native.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    profile = deepcopy(PROFILE)
    for row in profile["answers"]:
        row["question"] = MOCK_ANSWER_SLOTS[row["id"]]
    store.save_profile(profile)
    faults = {"posts": 0}
    server_app = create_mock_app(tmp_path / "receipts.json", scenario="native-" + request.param)

    @server_app.middleware("http")
    async def fault_injection(request, call_next):
        response = await call_next(request)
        content = b"".join([chunk async for chunk in response.body_iterator])
        if request.method == "POST" and request.url.path.endswith("/submit"):
            faults["posts"] += 1
        if request.method == "GET" and request.url.path.endswith("/B") and faults.get("html_change"):
            old, new = faults["html_change"]
            content = content.replace(old.encode(), new.encode())
        if "/ats/receipts/" in request.url.path and faults.get("receipt_change"):
            content = json.dumps({**json.loads(content), **faults["receipt_change"]}).encode()
        return Response(content, status_code=response.status_code,
                        headers={key: value for key, value in response.headers.items() if key != "content-length"})

    with patch("fixtures.mock_server.create_mock_app", return_value=server_app), \
            fixture_server(receipt_file=tmp_path / "receipts.json", scenario="native-" + request.param) as origin:
        discover(store, origin, "fixture")
        job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
        app_id = prepare(store, job["id"], "fixture", tmp_path)
        with TestClient(create_app(store, origin, tmp_path, operation_mode="FIXTURE")) as client:
            yield {"store": store, "origin": origin, "root": tmp_path, "app_id": app_id,
                   "client": client, "provider": request.param, "job_id": job["id"], "faults": faults}
    store.engine.dispose()


def run_native(case, request_id, *, submit=False):
    response = case["client"].post(f'/profiles/fixture/applications/{case["app_id"]}/mock-run',
                                   json={"submit": submit, "request_id": request_id})
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    execute_native_operation(case, operation_id)
    return case["store"].application(case["app_id"]), operation_id


def execute_native_operation(case, operation_id):
    with patch.dict(os.environ, {"MJOBS_DATABASE_URL": case["store"].url,
        "MJOBS_OUTPUT_DIR": str(case["root"]), "MJOBS_MOCK_ORIGIN": case["origin"],
        "MJOBS_OPERATION_MODE": "FIXTURE"}):
        execute_operation(operation_id)


def test_public_mock_run_uses_real_native_adapter_and_server_receipt(native_application):
    case = native_application
    dry, _ = run_native(case, "native-dry")
    assert dry["state"] == "DRY_RUN_COMPLETE", dry["trace"]
    assert case["faults"]["posts"] == 0
    assert httpx.get(case["origin"] + "/ats/receipts/" + case["app_id"]).json() == {"status": "not_found"}
    result, operation_id = run_native(case, "native-submit", submit=True)
    assert result["state"] == "CONFIRMED", result
    receipt = httpx.get(case["origin"] + "/ats/receipts/" + case["app_id"]).json()
    assert receipt == result["confirmation"]
    assert receipt["native_provider"] == case["provider"]
    assert receipt["request_id"] == operation_id
    assert receipt["submission_count"] == 1
    with case["store"].engine.connect() as connection:
        reviews = list(connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == case["app_id"], m.audit.c.action == "OWNED_ATS_EXECUTION_REVIEWED",
        )).scalars())
        uploads = list(connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == case["app_id"], m.audit.c.action == "APPLICATION_BROWSER_UPLOADS_VERIFIED_V1",
        )).scalars())
    reviewed = next(row for row in reviews if row["submit"] is True)
    assert reviewed["review_sha256"] == receipt["review_sha256"]
    assert {row["field_key"] for row in reviewed["contract"]["reviewed_fields"]} == set(MOCK_ANSWER_SLOTS)
    assert PROFILE["email"] not in json.dumps(reviews)
    assert len(uploads) == 2
    for manifest in uploads:
        assert {row["sha256"] for row in manifest["documents"]} == {receipt["resume_sha256"], receipt["cover_letter_sha256"]}
    # Replaying the durable request cannot launch the browser or send a second POST.
    with patch("services.applications.owned_ats_execution.sync_playwright", side_effect=AssertionError("replayed browser")):
        replay, repeated_id = run_native(case, "native-submit", submit=True)
    assert repeated_id == operation_id
    assert replay["confirmation"] == receipt
    assert case["faults"]["posts"] == 1
    history_path = f'/profiles/fixture/applications/{case["app_id"]}/attempt-history'
    history = case["client"].get(history_path).json()
    native_history = [row["native_fixture"] for row in history["attempts"]]
    assert len(native_history) == 2
    assert all("VALIDATE:COMPLETED" in row["steps"] for row in history["attempts"])
    submitted_review = next(row for row in native_history if row["request_id"] == operation_id)
    assert submitted_review["provider"] == case["provider"]
    assert submitted_review["review_sha256"] == receipt["review_sha256"]
    assert submitted_review["reviewed_fields"] == reviewed["contract"]["reviewed_fields"]
    assert PROFILE["email"] not in json.dumps(native_history)
    assert case["client"].get(history_path.replace("/fixture/", "/another-profile/")).status_code == 404
    # Unrelated application/attempt audits cannot supply this application's proof.
    with case["store"].engine.begin() as connection:
        case["store"]._audit(connection, "other-application", "OWNED_ATS_EXECUTION_REVIEWED", reviewed)
        case["store"]._audit(connection, case["app_id"], "OWNED_ATS_EXECUTION_REVIEWED",
                             {**reviewed, "attempt_id": "other-attempt"})
    changed_profile = case["store"].profile("fixture")
    changed_profile["answers"][-1]["text"] = "Future answer revision."
    case["store"].save_profile(changed_profile)
    assert case["client"].get(history_path).json() == history
    assert case["store"].application(case["app_id"])["confirmation"] == receipt
    with case["store"].engine.begin() as connection:
        case["store"]._audit(connection, case["app_id"], "OWNED_ATS_EXECUTION_REVIEWED", reviewed)
    ambiguous = case["client"].get(history_path).json()
    assert next(row for row in ambiguous["attempts"] if row["mode"] == "MOCK_SUBMISSION")["native_fixture"] is None


def save_answers_and_reprepare(case, answers, *, name=None, email=None):
    profile = deepcopy(PROFILE)
    current_profile = case["store"].profile("fixture")
    profile.update(name=current_profile["name"], email=current_profile["email"])
    profile["answers"] = answers
    if name is not None:
        profile["name"] = name
    if email is not None:
        profile["email"] = email
    profile["expected_version"] = case["store"].profile("fixture")["version"]
    saved = case["client"].put("/profiles", json=profile)
    assert saved.status_code == 200, saved.text
    application = case["store"].application(case["app_id"])
    response = case["client"].post(f'/profiles/fixture/applications/{case["app_id"]}/reprepare', json={
        "expected_profile_version": saved.json()["version"],
        "expected_application_profile_version": application["profile_version"],
        "expected_document_version": application["document_version"],
        "expected_state": application["state"],
    })
    assert response.status_code == 200, response.text


def test_onboarding_persona_completes_both_native_adapters_with_bound_profile_and_documents(native_application):
    from services.applications.owned_ats_execution import _hash
    case = native_application
    save_answers_and_reprepare(case, case["store"].profile("fixture")["answers"],
                               name="Synthetic Onboarding Applicant", email="onboarding@example.invalid")
    resume_operation, resume_path = queue_reviewed_resume(case)
    execute_native_operation(case, resume_operation)
    resumed = case["client"].get(resume_path + "/reviewed-resume")
    assert resumed.status_code == 200, resumed.text
    current_profile = case["store"].profile("fixture")
    current_application = case["store"].application(case["app_id"])
    assert (current_profile["name"], current_profile["email"]) == (
        "Synthetic Onboarding Applicant", "onboarding@example.invalid")
    assert current_application["state"] == "DRY_RUN_COMPLETE"
    assert case["faults"]["posts"] == 0
    confirmed, operation_id = run_native(case, "onboarding-native-submit", submit=True)
    assert confirmed["state"] == "CONFIRMED", confirmed["trace"]
    receipt = confirmed["confirmation"]
    assert receipt["native_provider"] == case["provider"]
    assert receipt["request_id"] == operation_id
    assert case["faults"]["posts"] == receipt["submission_count"] == 1
    history = case["client"].get(f'/profiles/fixture/applications/{case["app_id"]}/attempt-history').json()
    submitted = next(row for row in history["attempts"] if row["mode"] == "MOCK_SUBMISSION")
    assert submitted["profile_version"] == current_profile["version"]
    assert submitted["document_version"] == current_application["document_version"]
    assert submitted["native_fixture"]["profile_sha256"] == _hash(current_profile)
    assert submitted["native_fixture"]["review_sha256"] == receipt["review_sha256"]
    assert {row["sha256"] for row in submitted["verified_uploads"]} == {receipt["resume_sha256"], receipt["cover_letter_sha256"]}


def queue_reviewed_resume(case):
    answers = deepcopy(case["store"].profile("fixture")["answers"])
    save_answers_and_reprepare(case, [row for row in answers if row["id"] != "fixture_question"])
    unknown, _ = run_native(case, "unknown-question")
    assert unknown["state"] == "REVIEW_REQUIRED"
    assert case["faults"]["posts"] == 0
    base = f'/profiles/fixture/applications/{case["app_id"]}/answer-review-resumes'
    response = case["client"].get(base + "/questions/fixture_question")
    assert response.status_code == 200, response.text
    review = response.json()
    save_answers_and_reprepare(case, answers)
    response = case["client"].post(base, json={
        "request_id": "reviewed-resume", "question_id": "fixture_question",
        "question_fingerprint": review["question"]["fingerprint"],
        "form_fingerprint": review["form_fingerprint"], "answer_id": "fixture_question",
        "expected_profile_version": case["store"].profile("fixture")["version"],
    })
    assert response.status_code == 202, response.text
    return response.json()["operation_id"], base


def test_unknown_question_requires_bound_approved_resume_without_post(native_application):
    case = native_application
    operation_id, base = queue_reviewed_resume(case)
    execute_native_operation(case, operation_id)
    completed = case["client"].get(base + "/reviewed-resume")
    assert completed.status_code == 200, completed.text
    assert completed.json()["preflight"] == {"result": "PASS", "is_current": True}
    assert case["store"].application(case["app_id"])["state"] == "DRY_RUN_COMPLETE"
    assert case["faults"]["posts"] == 0
    with case["store"].engine.connect() as connection:
        observed = list(connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == case["app_id"], m.audit.c.action == "OWNED_ATS_FORM_OBSERVED",
        )).scalars())
    assert observed and all(row["analysis"]["status"] == "PASS" for row in observed)
    assert PROFILE["email"] not in json.dumps(observed)


@pytest.mark.parametrize("change", ["template", "question", "sensitive", "barrier", "profile", "document"])
def test_queued_review_rejects_changed_inputs_without_post(native_application, change):
    case = native_application
    operation_id, base = queue_reviewed_resume(case)
    if change == "template":
        case["faults"]["html_change"] = ("<form ", '<form data-revision="changed" ')
    elif change == "barrier":
        case["faults"]["html_change"] = ("<form ", '<form data-intervention="CAPTCHA" ')
    elif change in {"question", "sensitive"}:
        label = "Another question" if change == "question" else "Social security number"
        case["faults"]["html_change"] = ("Approved fixture answer", label)
    elif change == "profile":
        profile = case["store"].profile("fixture")
        profile["answers"][-1]["text"] = "Changed after approval."
        case["store"].save_profile(profile)
    else:
        application = case["store"].application(case["app_id"])
        with case["store"].engine.connect() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == case["app_id"], m.documents.c.kind == "resume",
                m.documents.c.version == application["document_version"])).mappings().one()
        path = case["root"] / case["app_id"] / document["files"]["pdf"]
        path.write_bytes(path.read_bytes() + b"changed after approval")
    execute_native_operation(case, operation_id)
    assert case["client"].get(base + "/reviewed-resume").status_code == 409
    assert case["faults"]["posts"] == 0
    assert case["store"].application(case["app_id"])["state"] != "DRY_RUN_COMPLETE"


def test_api_reconciliation_rejects_conflicting_native_receipts_and_attempts(native_application):
    case = native_application
    case["faults"]["receipt_change"] = {"review_sha256": "0" * 64}
    unconfirmed, _ = run_native(case, "uncertain-native-submit", submit=True)
    assert unconfirmed["state"] == "SUBMISSION_UNCONFIRMED", unconfirmed["trace"]
    assert case["faults"]["posts"] == 1
    path = f'/profiles/fixture/applications/{case["app_id"]}/confirmation-check'
    with patch("services.applications.owned_ats_execution.sync_playwright", side_effect=AssertionError("recovery browser")):
        for index, key in enumerate(("review_sha256", "contract_sha256", "request_id",
                                     "resume_sha256", "cover_letter_sha256", "native_provider")):
            case["faults"]["receipt_change"] = {key: "0" * 64}
            response = case["client"].post(path, json={"request_id": f"reject-{index}"})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "NO_VERIFIED_RECEIPT", response.text
            assert case["store"].application(case["app_id"])["state"] == "SUBMISSION_UNCONFIRMED"
        # A receipt that copies an altered review digest still cannot invent an
        # existing owned submitting attempt and its immutable input snapshot.
        from services.applications.owned_ats_execution import _hash
        with case["store"].engine.begin() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == case["app_id"], m.audit.c.action == "OWNED_ATS_EXECUTION_REVIEWED",
            )).mappings().one()
            original = event["payload"]
            changed = {**original, "attempt_id": "nonexistent-attempt"}
            changed["review_sha256"] = _hash({key: value for key, value in changed.items() if key != "review_sha256"})
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(payload=changed))
        case["faults"]["receipt_change"] = {"review_sha256": changed["review_sha256"]}
        response = case["client"].post(path, json={"request_id": "reject-attempt"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "NO_VERIFIED_RECEIPT"
        with case["store"].engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(payload=original))
        case["faults"].pop("receipt_change")
        response = case["client"].post(path, json={"request_id": "actual-receipt"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CONFIRMED"
    assert case["faults"]["posts"] == 1


def test_actual_native_validation_rejects_invalid_filled_control_before_upload(native_application):
    case = native_application
    case["faults"]["html_change"] = ('name="email" type="email" required',
                                       'name="email" type="email" pattern="cannot-match" required')
    result, _ = run_native(case, "native-invalid-control", submit=True)
    assert result["state"] == "REVIEW_REQUIRED", result["trace"]
    assert case["faults"]["posts"] == 0
    with case["store"].engine.connect() as connection:
        manifests = connection.execute(select(m.audit.c.id).where(
            m.audit.c.entity_id == case["app_id"], m.audit.c.action == "APPLICATION_BROWSER_UPLOADS_VERIFIED_V1",
        )).all()
    assert manifests == []
