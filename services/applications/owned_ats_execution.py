"""Native ATS fixture execution inside the durable MOCK application lifecycle.

The selected route remains mock-ats. Public provider URLs below identify the
adapter's fictional metadata contract only and are never network destinations.
Browser traffic uses one exact owned-origin mapping and server-created receipts.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from packages.persistence import models as m
from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.mock_answer_bindings import (
    MOCK_ANSWER_SLOTS,
    bind_mock_answer_slots,
)
from services.applications.owned_ats_fixture import (
    OwnedATSFixtureError,
    freeze_fixture,
    freeze_forwarding,
    open_fixture_facade,
    prepare_reviewed_fixture,
)
from services.applications.preflight import questionnaire_fingerprint
from services.applications.upload_integrity import (
    record_verified_uploads,
    snapshot_prepared,
)
from services.audit_history import record_mock_application_submitted

_VIRTUAL_ORIGIN = "http://127.0.0.1:18792"
_REVIEW_ACTION = "OWNED_ATS_EXECUTION_REVIEWED"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LABELS = {"name": "Name", "email": "Email", "resume": "Resume",
           "cover_letter": "Cover letter", **MOCK_ANSWER_SLOTS}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def native_route(route, origin):
    """Select only the explicit native path; never upgrade a public route."""
    url = route.get("url", "")
    if not isinstance(url, str) or "/ats/native" not in url:
        return None
    match = re.fullmatch(re.escape(origin) + r"/ats/native/(greenhouse|lever)/(A|B)", url)
    if (match is None or route.get("provider") != "mock-ats" or route.get("tenant") != "example"
            or route.get("requisition") != match[2]):
        raise OwnedATSFixtureError("OWNED_ATS_NATIVE_ROUTE_INVALID")
    return {"provider": match[1], "requisition": match[2], "path": urlsplit(url).path}


def _forwarding(native, origin, application_id, request_id, *, submit):
    routes = [("GET", native["path"])]
    if submit:
        routes.append(("POST", native["path"] + "/submit"))
    return {"workspace_mode": "FIXTURE", "application_id": application_id,
            "request_id": request_id, "virtual_origin": _VIRTUAL_ORIGIN,
            "upstream_origin": origin, "routes": routes}


def _adapter(native):
    return GreenhouseApplicationAdapter() if native["provider"] == "greenhouse" else LeverApplicationAdapter()


def _timeout(check, maximum=10000):
    check()
    remaining = getattr(check, "remaining_seconds", None)
    return max(1, min(maximum, int(remaining() * 1000))) if callable(remaining) else maximum


def _observe(page, native, check):
    check()
    if page.locator("[data-auth-required], [data-intervention]").count():
        raise OwnedATSFixtureError("OWNED_ATS_INTERVENTION_REQUIRED")
    analysis = _adapter(native).analyze_page(page, check=check)
    fields = analysis.get("fields", [])
    if (analysis.get("status") != "PASS" or len(fields) != len(_LABELS)
            or {field.get("name") for field in fields} != set(_LABELS)
            or any(field.get("sensitivity") != "STANDARD" or field.get("ambiguous") is not False
                   or field.get("aria_label") != _LABELS[field["name"]] for field in fields)):
        raise OwnedATSFixtureError("OWNED_ATS_FORM_REVIEW_REQUIRED")
    shape = page.locator("form").evaluate("""form => ({
        action: form.getAttribute('action'), method: form.getAttribute('method'),
        enctype: form.getAttribute('enctype'),
        controls: Array.from(form.querySelectorAll('input,textarea,select')).map(n => ({
            name: n.name, required: n.required, disabled: n.disabled}))})""",
        timeout=_timeout(check))
    if (shape["action"] != native["path"] + "/submit" or shape["method"] != "post"
            or shape["enctype"] != "multipart/form-data"
            or any(row["required"] is not True or row["disabled"] is not False for row in shape["controls"])):
        raise OwnedATSFixtureError("OWNED_ATS_FORM_REVIEW_REQUIRED")
    template = page.content()
    return template, analysis, {"provider": native["provider"],
                               "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                               "analysis_sha256": _hash(analysis), "analysis": analysis}


def read_native_questionnaire(client, route):
    """Read metadata and the actual native form, with GET-only browser authority."""
    native = native_route(route, client.origin)
    response = client.get(route["url"] + "/questions")
    if response.status_code != 200:
        raise OwnedATSFixtureError("OWNED_ATS_QUESTIONNAIRE_UNAVAILABLE")
    questionnaire = response.json()
    if (not isinstance(questionnaire, dict) or questionnaire.get("template_version") != "native-form-v1"
            or not isinstance(questionnaire.get("contract_sha256"), str)
            or not _HASH.fullmatch(questionnaire["contract_sha256"])):
        raise OwnedATSFixtureError("OWNED_ATS_QUESTIONNAIRE_UNAVAILABLE")
    forwarding = _forwarding(native, client.origin, "metadata-observation", "metadata-observation", submit=False)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"],
                                             timeout=_timeout(client.check, 15000))
        try:
            with open_fixture_facade(browser, freeze_forwarding(**forwarding), current=forwarding,
                                     check=client.check) as facade:
                page = facade.context.new_page()
                result = page.goto(_VIRTUAL_ORIGIN + native["path"], wait_until="domcontentloaded",
                                   timeout=_timeout(facade))
                facade()
                if result is None or result.status != 200:
                    raise OwnedATSFixtureError("OWNED_ATS_FORM_UNAVAILABLE")
                _, _, observed = _observe(page, native, facade)
                return {**questionnaire, "owned_native": observed}
        finally:
            browser.close()


def resume_fingerprint(store, request_id):
    if request_id is None:
        return None
    with store.engine.connect() as connection:
        params = connection.execute(select(m.operations.c.params).where(
            m.operations.c.id == request_id)).scalar_one_or_none()
    return (params or {}).get("resume_binding", {}).get("form_fingerprint")


def read_native_reviews(connection, application_id, attempt_ids):
    """Read immutable value-free proof only for these owned historical attempts."""
    from services.applications.attempt_history import attempt_id_for_request

    if not attempt_ids:
        return {}
    rows = list(connection.execute(select(m.audit.c.payload).where(
        m.audit.c.entity_id == application_id, m.audit.c.action == _REVIEW_ACTION,
        m.audit.c.payload["attempt_id"].as_string().in_(attempt_ids),
    ).limit(101)).scalars())
    if len(rows) > 100:
        return {}
    result = {}
    seen = set()
    for payload in rows:
        try:
            attempt_id = payload["attempt_id"]
            if attempt_id in seen:
                result.pop(attempt_id, None)
                continue
            seen.add(attempt_id)
            contract = payload["contract"]
            if (contract["application_id"] != application_id
                    or contract["provider"] not in {"greenhouse", "lever"}
                    or not isinstance(payload["request_id"], str)
                    or attempt_id != attempt_id_for_request(application_id, payload["request_id"])
                    or payload["review_sha256"] != _hash({
                        key: value for key, value in payload.items() if key != "review_sha256"})):
                continue
            projection = {key: payload[key] for key in (
                "request_id", "job_id", "review_sha256", "server_contract_sha256",
                "metadata_fingerprint", "profile_sha256", "route_sha256")}
            projection.update(provider=contract["provider"], template_sha256=contract["template_sha256"],
                              analysis_sha256=contract["analysis_sha256"])
            if any(not isinstance(value, str) or not _HASH.fullmatch(value)
                   for key, value in projection.items() if key.endswith("sha256") or key == "metadata_fingerprint"):
                continue
            bindings = contract["reviewed_fields"]
            if (not isinstance(bindings, list) or len(bindings) != len(MOCK_ANSWER_SLOTS)
                    or {row["field_key"] for row in bindings} != set(MOCK_ANSWER_SLOTS)
                    or any(set(row) != {"field_key", "field_sha256", "answer_id", "answer_sha256"}
                           or not isinstance(row["answer_id"], str)
                           or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["answer_id"])
                           or not _HASH.fullmatch(row["field_sha256"])
                           or not _HASH.fullmatch(row["answer_sha256"]) for row in bindings)):
                continue
            projection["reviewed_fields"] = bindings
            result[attempt_id] = projection
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _review_profile(profile):
    """Keep the existing explicit legacy slot fallback, bound to the raw profile."""
    result = deepcopy(profile)
    bindings = bind_mock_answer_slots(profile.get("answers", []))
    reviews = []
    for key, fact in bindings.items():
        answer = next(row for row in result["answers"] if row["id"] == fact.id)
        if answer.get("question") is None and answer["id"] == key:
            answer["question"] = MOCK_ANSWER_SLOTS[key]
        reviews.append({"field_key": key, "answer_id": fact.id})
    return result, reviews


def _fixture_route(native):
    if native["provider"] == "greenhouse":
        return "https://boards.greenhouse.io/example/jobs/" + ("1001" if native["requisition"] == "A" else "1002")
    suffix = "000000000001" if native["requisition"] == "A" else "000000000002"
    return "https://jobs.lever.co/example/00000000-0000-4000-8000-" + suffix


def validate_native_receipt(store, application, route, origin, receipt):
    """Match real server evidence to the exact persisted submitting attempt."""
    native = native_route(route, origin)
    if native is None or not isinstance(receipt, dict):
        return receipt if native is None else None
    with store.engine.connect() as connection:
        payload = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == application["id"], m.audit.c.action == _REVIEW_ACTION,
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).scalar_one_or_none()
    if not isinstance(payload, dict) or payload.get("submit") is not True:
        return None
    from services.applications.attempt_history import (
        STARTED,
        _event_id,
        attempt_id_for_request,
    )
    if (not isinstance(payload.get("request_id"), str)
            or payload.get("attempt_id") != attempt_id_for_request(application["id"], payload["request_id"])):
        return None
    with store.engine.connect() as connection:
        attempt = connection.execute(select(m.attempts).where(
            m.attempts.c.id == payload["attempt_id"],
            m.attempts.c.application_id == application["id"])).mappings().one_or_none()
        started = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.id == _event_id(STARTED, payload["attempt_id"]),
            m.audit.c.entity_id == application["id"])).scalar_one_or_none()
    if attempt is None or not isinstance(started, dict):
        return None
    snapshot = started.get("snapshot", {})
    if (snapshot.get("mode") != "MOCK_SUBMISSION" or snapshot.get("provider") != "mock-ats"
            or any(snapshot.get(key) != application[key] for key in
                   ("route_id", "profile_id", "profile_version", "document_version"))
            or snapshot.get("route_url_sha256") != hashlib.sha256(route["url"].encode()).hexdigest()):
        return None
    contract = payload.get("contract", {})
    if (not isinstance(contract, dict) or not isinstance(contract.get("documents"), list)
            or len(contract["documents"]) != 2
            or any(not isinstance(row, dict) or row.get("kind") not in {"resume", "cover-letter"}
                   or not isinstance(row.get("sha256"), str) for row in contract["documents"])):
        return None
    expected = {"native_provider": native["provider"], "request_id": payload.get("request_id"),
                "contract_sha256": payload.get("server_contract_sha256"),
                "review_sha256": payload.get("review_sha256"),
                "confirmation_url": origin + native["path"] + "/submit"}
    documents = {row["kind"]: row["sha256"] for row in contract.get("documents", [])}
    if documents != {row["kind"]: row["sha256"].get("pdf") for row in snapshot.get("documents", [])}:
        return None
    expected.update(resume_sha256=documents.get("resume"), cover_letter_sha256=documents.get("cover-letter"))
    if (payload.get("route_sha256") != _hash(route) or payload.get("review_sha256") != _hash({
            key: value for key, value in payload.items() if key != "review_sha256"})
            or payload.get("job_id") != application["job_id"]
            or contract.get("application_id") != application["id"]
            or contract.get("profile_id") != application["profile_id"]
            or contract.get("profile_version") != application["profile_version"]
            or contract.get("document_version") != application["document_version"]
            or type(receipt.get("submission_count")) is not int or receipt["submission_count"] != 1
            or any(value is None or receipt.get(key) != value for key, value in expected.items())):
        return None
    return receipt


def execute_native(*, store, application, route, client, root, profile, documents,
                   upload_snapshots, preflight, attempt_id, request_id, submit,
                   confidence, check, start_step, finish_step):
    """Run actual adapters behind existing preflight, reservation and consent gates."""
    from services.applications.mock_adapter import _authorize, _receipt

    store.assert_workspace_mode("FIXTURE")
    native = native_route(route, client.origin)
    request_id = request_id or attempt_id
    forwarding = _forwarding(native, client.origin, application["id"], request_id, submit=submit)
    frozen_forwarding = freeze_forwarding(**forwarding)
    reviewed_profile, reviewed_fields = _review_profile(profile)

    def current_check():
        check()
        current = store.application(application["id"])
        if (any(current[key] != application[key] for key in
                ("target", "route_id", "profile_id", "profile_version", "document_version"))
                or store.profile(application["profile_id"]) != profile):
            raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")
        with store.engine.connect() as connection:
            current_route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
            current_documents = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == application["id"],
                m.documents.c.version == application["document_version"])).mappings())
        if (current_route != route
                or sorted(current_documents, key=lambda row: row["id"])
                != sorted(documents, key=lambda row: row["id"])):
            raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")
        for document in current_documents:
            current_file = (root / document["files"]["pdf"]).resolve()
            if not current_file.is_relative_to(root):
                raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")
            if snapshot_prepared(document, current_file) != upload_snapshots[document["kind"]]:
                raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")

    if callable(getattr(check, "remaining_seconds", None)):
        current_check.remaining_seconds = check.remaining_seconds
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"],
                                             timeout=_timeout(current_check, 15000))
        try:
            with open_fixture_facade(browser, frozen_forwarding, current=forwarding,
                                     check=current_check) as facade:
                page = facade.context.new_page()
                page.set_default_timeout(_timeout(facade))
                start_step("NAVIGATE")
                store.transition(application["id"], [application["state"]], "FILLING", "NAVIGATE")
                response = page.goto(_VIRTUAL_ORIGIN + native["path"], wait_until="domcontentloaded",
                                     timeout=_timeout(facade))
                facade()
                if response is None or response.status != 200:
                    raise OwnedATSFixtureError("OWNED_ATS_FORM_UNAVAILABLE")
                finish_step("COMPLETED")
                start_step("DETECT_FORM")
                template, analysis, observed = _observe(page, native, facade)
                questionnaire = client.get(route["url"] + "/questions").json()
                if questionnaire_fingerprint({**questionnaire, "owned_native": observed}) != preflight["metadata_fingerprint"]:
                    raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")
                current = {"workspace_mode": "FIXTURE", "application_id": application["id"],
                           "provider": native["provider"], "route": {"provider": native["provider"],
                                                                     "url": _fixture_route(native)},
                           "origin": _VIRTUAL_ORIGIN, "page_url": page.url, "template": template,
                           "analysis": analysis, "profile": reviewed_profile,
                           "document_version": application["document_version"],
                           "documents": list(upload_snapshots.values()), "reviewed_fields": reviewed_fields}
                contract = freeze_fixture(**current)
                payload = {"attempt_id": attempt_id, "request_id": request_id, "submit": submit,
                           "job_id": application["job_id"], "route_sha256": _hash(route),
                           "profile_sha256": _hash(profile), "contract": asdict(contract),
                           "forwarding": asdict(frozen_forwarding),
                           "metadata_fingerprint": preflight["metadata_fingerprint"],
                           "server_contract_sha256": questionnaire["contract_sha256"]}
                payload["review_sha256"] = _hash(payload)
                facade()
                with store.engine.begin() as connection:
                    store._audit(connection, application["id"], _REVIEW_ACTION, payload)
                finish_step("COMPLETED")
                start_step("FILL_CONTACT")
                for field in ("name", "email"):
                    page.locator(f'[name="{field}"]').fill(profile[field], timeout=_timeout(facade))
                    if page.locator(f'[name="{field}"]').input_value() != profile[field]:
                        raise OwnedATSFixtureError("OWNED_ATS_ANSWER_READBACK_MISMATCH")
                finish_step("COMPLETED")
                start_step("UPLOAD_RESUME")
                result = prepare_reviewed_fixture(page, contract, current=current, check=facade)
                record_verified_uploads(store, application["id"], attempt_id, result["documents"])
                finish_step("COMPLETED")
                start_step("VALIDATE")
                facade()
                if page.locator("form").evaluate("form => form.checkValidity()", timeout=_timeout(facade)) is not True:
                    raise OwnedATSFixtureError("OWNED_ATS_FORM_INVALID")
                store.transition(application["id"], ["FILLING"], "READY_TO_SUBMIT", "VALIDATE")
                finish_step("COMPLETED")
                if not submit:
                    store.transition(application["id"], ["READY_TO_SUBMIT"], "DRY_RUN_COMPLETE", "STOP_BEFORE_SUBMIT")
                    return store.application(application["id"])
                if page.content() != template or _adapter(native).analyze_page(page, check=facade) != analysis:
                    raise OwnedATSFixtureError("OWNED_ATS_REVIEW_STALE")
                # The controlled listener adds execution context only at the native
                # submit event; no hidden controls contaminate the reviewed form.
                page.locator("form").evaluate("""(form, binding) => form.addEventListener('formdata', event => {
                    for (const [key, value] of Object.entries(binding)) event.formData.set(key, value);
                }, {once: true})""", {"application_id": application["id"], "request_id": request_id,
                                      "contract_sha256": questionnaire["contract_sha256"],
                                      "review_sha256": payload["review_sha256"]}, timeout=_timeout(facade))
                decision = _authorize(store, application["id"], confidence, check=facade)
                if not decision.allowed:
                    store.transition(application["id"], ["READY_TO_SUBMIT"], "REVIEW_REQUIRED", "POLICY_GATE")
                    return store.application(application["id"])
                start_step("SUBMIT")
                facade()
                page.get_by_role("button", name="Submit owned fixture", exact=True).click(timeout=_timeout(facade))
                facade()
                record_mock_application_submitted(store, application["id"])
                finish_step("COMPLETED")
                start_step("VERIFY_CONFIRMATION")
                confirmation = page.locator("[data-confirmation]")
                confirmation.wait_for(timeout=_timeout(facade))
                receipt = _receipt(client, application, route["requisition"],
                    observed_url=facade.upstream_url(page.url, method="POST"),
                    observed_text=" ".join(page.locator("body").inner_text().split()),
                    observed_reference=confirmation.get_attribute("data-confirmation"))
                receipt = validate_native_receipt(store, application, route, client.origin, receipt)
                if receipt is None:
                    raise OwnedATSFixtureError("OWNED_ATS_RECEIPT_INVALID")
                store.transition(application["id"], ["SUBMITTING"], "CONFIRMED", "VERIFY_CONFIRMATION", receipt)
                finish_step("COMPLETED")
                return store.application(application["id"])
        finally:
            browser.close()
