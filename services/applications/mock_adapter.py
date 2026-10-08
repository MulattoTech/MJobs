"""Controlled real browser flow for the explicit local mock ATS provider."""
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
from uuid import uuid4
import httpx

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select, update

from packages.domain.safety import Fact, Policy, SubmissionContext, evaluate_submission, select_verified_facts
from packages.persistence import models as m
from packages.persistence.store import now
from services.discovery.pipeline import MockClient
from services.audit_history import record_mock_application_submitted
from services.applications.preflight import (
    build_preflight_result,
    persist_preflight,
    questionnaire_fingerprint,
)
from services.applications.debug_capture import (
    append_diagnostic_event,
    append_console_error,
    capture_browser_failure,
)
from services.documents.errors import PreparationError
from services.documents.validation import validate_stored_resume, VALIDATOR
from services.documents.avoid_list import validate_content as validate_avoid_list


def _playwright_action_timeout_ms(check, maximum_ms=10000):
    remaining_seconds = getattr(check, "remaining_seconds", None)
    if not callable(remaining_seconds):
        check()
        return maximum_ms
    remaining = remaining_seconds()
    if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
        check()
        raise ValueError("OPERATION_BUDGET_INVALID")
    timeout_ms = int(remaining * 1000)
    if timeout_ms < 1:
        check()
        raise ValueError("OPERATION_TIMEOUT")
    return min(maximum_ms, timeout_ms)


def _receipt(client, application, requisition, *, observed_url=None,
             observed_text=None, observed_reference=None):
    try:
        receipt = client.get(client.origin + "/ats/receipts/" + application["id"]).json()
    except (httpx.HTTPError, ValueError):
        return None
    except Exception:
        try:
            client.check()
        except Exception:
            return None
        raise
    if not (isinstance(receipt, dict) and receipt.get("application_id") == application["id"]
            and receipt.get("requisition") == requisition
            and isinstance(receipt.get("receipt"), str)
            and receipt["receipt"].startswith("MOCK-")):
        return None
    reference = receipt.get("reference_number")
    confirmation_text = receipt.get("confirmation_text")
    confirmation_url = receipt.get("confirmation_url")
    captured_at = receipt.get("timestamp")
    if (not isinstance(reference, str) or not reference or reference != receipt["receipt"]
            or not isinstance(confirmation_text, str) or not confirmation_text.strip()
            or len(confirmation_text) > 10000
            or not isinstance(confirmation_url, str) or not client.allowed(confirmation_url)
            or not isinstance(captured_at, str)):
        return None
    try:
        timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    normalized_text = " ".join(confirmation_text.split()).casefold()
    if not any(phrase in normalized_text for phrase in (
            "thank you for applying", "application received",
            "we've received your application", "confirmation id", "application reference")):
        return None
    if reference.casefold() not in normalized_text:
        return None
    if observed_url is not None and confirmation_url != observed_url:
        return None
    if observed_text is not None and " ".join(observed_text.split()) != " ".join(confirmation_text.split()):
        return None
    if observed_reference is not None and observed_reference != reference:
        return None
    return receipt


def _authorize(store, application_id, confidence, policy=Policy(), check=None):
    with store.engine.begin() as connection:
        application = connection.execute(select(m.applications).where(m.applications.c.id == application_id).with_for_update()).mappings().one()
        # Serialize all submission reservations for the same profile in PostgreSQL.
        connection.execute(select(m.profiles).where(m.profiles.c.id == application["profile_id"]).with_for_update()).one()
        if check:
            check()
        job = connection.execute(select(m.jobs).where(m.jobs.c.id == application["job_id"])).mappings().one()
        match = connection.execute(select(m.matches).where(m.matches.c.job_id == job["id"], m.matches.c.profile_id == application["profile_id"],
            m.matches.c.profile_version == application["profile_version"])).mappings().one()
        submitted = select(func.count()).select_from(m.applications.join(m.jobs, m.jobs.c.id == m.applications.c.job_id)).where(
            m.applications.c.profile_id == application["profile_id"],
            m.applications.c.state.in_(["SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"]))
        timestamp = datetime.now(timezone.utc)
        hourly = connection.execute(submitted.where(m.applications.c.submitted_at >= (timestamp - timedelta(hours=1)).isoformat())).scalar_one()
        daily = connection.execute(submitted.where(m.applications.c.submitted_at >= (timestamp - timedelta(days=1)).isoformat())).scalar_one()
        company = connection.execute(submitted.where(m.jobs.c.company_id == job["company_id"])).scalar_one()
        decision = evaluate_submission(SubmissionContext(target=application["target"], state=application["state"],
            match_score=match["score"], confidence=confidence, quality_approved=True, facts_verified=True,
            source_allowed=application["target"] == "MOCK", sensitive_answers_approved=True,
            hourly_count=hourly, daily_count=daily, company_count=company), policy)
        if decision.allowed:
            changed = connection.execute(update(m.applications).where(m.applications.c.id == application_id,
                m.applications.c.state == "READY_TO_SUBMIT").values(state="SUBMITTING", submitted_at=now(), trace=[*application["trace"], "SUBMIT"]))
            if changed.rowcount != 1:
                raise ValueError("Submission was claimed concurrently")
            store._audit(connection, application_id, "SUBMITTING", {"provider": "mock-ats"})
        return decision


def _read_questionnaire(client, route):
    """Read only structured metadata from the allowlisted local mock service."""
    if route.get("provider") != "mock-ats" or route.get("tenant") != "example":
        return None
    requisition = route.get("requisition")
    if not isinstance(requisition, str) or not requisition.isalnum() or len(requisition) > 80:
        return None
    try:
        response = client.get(f"{client.origin}/ats/questions/{requisition}")
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _preflight(store, application, route, profile, document_kinds, client):
    questionnaire = _read_questionnaire(client, route)
    result = build_preflight_result(profile, route, questionnaire, document_kinds,
                                    application_id=application["id"])
    result["document_version"] = application["document_version"]
    persist_preflight(store, application["id"], result)
    if result["result"] != "PASS":
        store.transition(application["id"], [application["state"]], "REVIEW_REQUIRED",
                         "QUESTION_PREFLIGHT_" + result["result"])
        return result

    # The browser must not start if the public metadata changed after evaluation.
    latest = _read_questionnaire(client, route)
    if (latest is None or questionnaire_fingerprint(latest) != result["metadata_fingerprint"]):
        stale = build_preflight_result(profile, route, latest, document_kinds,
                                       application_id=application["id"])
        stale.update({"reason": "STALE_PREFLIGHT", "result": "UNKNOWN",
                      "document_version": application["document_version"]})
        persist_preflight(store, application["id"], stale)
        store.transition(application["id"], [application["state"]], "REVIEW_REQUIRED",
                         "QUESTION_PREFLIGHT_STALE")
        return stale
    return result


def apply_mock(store, application_id, mock_origin, output_dir, submit=False, check=None,
               debug_capture=False):
    check = check or (lambda: None)
    if type(submit) is not bool:
        raise ValueError("Mock submission selection must be explicit boolean")
    if type(debug_capture) is not bool:
        raise ValueError("Browser debug capture selection must be explicit boolean")
    client = MockClient(mock_origin, check=check)
    try:
        application = store.application(application_id)
        with store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(m.routes.c.id == application["route_id"])).mappings().one())
            documents = list(connection.execute(select(m.documents).where(m.documents.c.application_id == application_id,
                m.documents.c.version == application["document_version"])).mappings())
        if application["target"] != "MOCK" or route["provider"] != "mock-ats" or not client.allowed(route["url"]):
            raise ValueError("This adapter supports only the configured local mock origin")
        if application["state"] == "CONFIRMED":
            return application
        if application["state"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
            receipt = _receipt(client, application, route["requisition"])
            if receipt:
                record_mock_application_submitted(store, application_id)
                store.transition(application_id, [application["state"]], "CONFIRMED", "VERIFY_CONFIRMATION", receipt)
            elif application["state"] == "SUBMITTING":
                store.transition(application_id, ["SUBMITTING"], "SUBMISSION_UNCONFIRMED")
            return store.application(application_id)
        if application["state"] not in ("PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"):
            return application
        check()
        profile = store.profile(application["profile_id"], application["profile_version"])
        current_profile = store.profile(application["profile_id"])
        root = Path(output_dir).resolve() / application_id
        files = {}
        for document in documents:
            for extension, filename in document["files"].items():
                path = (root / filename).resolve()
                if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"][extension]:
                    raise ValueError("Prepared document integrity failed")
                files[(document["kind"], extension)] = path
        if {document["kind"] for document in documents} != {"resume", "cover-letter"}:
            raise ValueError("Prepared document set incomplete")
        try:
            for document in documents:
                text_path = files.get((document['kind'], 'txt'))
                if text_path is not None:
                    validate_avoid_list(text_path.read_text(encoding='utf-8'),
                        current_profile.get('preferences'),
                        'resume' if document['kind'] == 'resume' else 'cover letter')
        except PreparationError as error:
            store.transition(application_id, [application['state']], 'REVIEW_REQUIRED', error.code)
            raise
        resume = next(document for document in documents if document['kind']=='resume')
        try:
            validate_stored_resume({ext:path for (kind,ext),path in files.items() if kind=='resume'}, profile, resume['fact_ids'], resume['template'], resume['generation_model'])
        except PreparationError as error:
            store.transition(application_id, [application['state']], 'REVIEW_REQUIRED', error.code)
            raise
        with store.engine.begin() as connection:
            store._audit(connection, application_id, 'RESUME_VALIDATED_BEFORE_USE',
                         {'validator': VALIDATOR, 'document_id': resume['id'], 'sha256': resume['sha256']})
        preflight = _preflight(store, application, route, profile,
                               {document["kind"] for document in documents}, client)
        if preflight["result"] != "PASS":
            return store.application(application_id)
        # Question-aware reusable-answer metadata is outside the legacy mock
        # form's Fact contract; only its original safety fields reach policy.
        answers = select_verified_facts([Fact(**{key: row[key] for key in
            ("id", "text", "source", "verified", "user_approved", "confidence")})
            for row in profile["answers"]], ["country", "remote", "fixture_question"])
        answer = {fact.id: fact.text for fact in answers}
        facts = select_verified_facts([Fact(id=row["fact_id"], **{key: row[key] for key in ("text", "source", "verified", "user_approved", "confidence")})
                                      for row in profile["facts"]], application["fact_ids"])
        attempt_id = uuid4().hex
        with store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(id=attempt_id, application_id=application_id, state="RUNNING", steps=[], started_at=now()))
        page = None
        browser_events = []
        browser_failure_handled = False
        active_step = None

        def append_attempt_step(step, status):
            # Persist only the fixed phase label and status. Never retain form
            # values, answer text, browser content, or exception messages here.
            with store.engine.begin() as connection:
                saved = connection.execute(select(m.attempts.c.steps).where(
                    m.attempts.c.id == attempt_id).with_for_update()).scalar_one()
                steps = list(saved or [])
                steps.append(f"{step}:{status}")
                connection.execute(update(m.attempts).where(
                    m.attempts.c.id == attempt_id).values(steps=steps))

        def start_step(step):
            nonlocal active_step
            if active_step is not None:
                raise RuntimeError("APPLICATION_TRACE_STEP_ALREADY_ACTIVE")
            append_attempt_step(step, "STARTED")
            active_step = step

        def finish_step(status):
            nonlocal active_step
            if active_step is None:
                return
            step = active_step
            with store.engine.begin() as connection:
                saved = connection.execute(select(m.attempts.c.steps).where(
                    m.attempts.c.id == attempt_id).with_for_update()).scalar_one()
                steps = list(saved or [])
                started = f"{step}:STARTED"
                if steps and steps[-1] == started:
                    steps[-1] = f"{step}:{status}"
                else:
                    steps.append(f"{step}:{status}")
                connection.execute(update(m.attempts).where(
                    m.attempts.c.id == attempt_id).values(steps=steps))
            active_step = None

        def handle_browser_failure(error):
            nonlocal browser_failure_handled
            current = store.application(application_id)["state"]
            trace = store.application(application_id).get("trace") or []
            # The in-flight phase identifies where this attempt actually
            # stopped; fall back to the last transition for failures between
            # recorded phases.
            failed_step = active_step or (trace[-1] if trace else "UNKNOWN")
            if active_step is not None:
                finish_step("FAILED")
            if current == "SUBMITTING":
                receipt = _receipt(client, application, route["requisition"])
                if receipt:
                    record_mock_application_submitted(store, application_id)
                store.transition(application_id, ["SUBMITTING"], "CONFIRMED" if receipt else "SUBMISSION_UNCONFIRMED",
                                 "RECONCILE_CONFIRMATION", receipt)
            elif current in ("FILLING", "READY_TO_SUBMIT"):
                store.transition(application_id, [current], "REVIEW_REQUIRED", "FORM_OR_DOCUMENT_ERROR")
            if debug_capture:
                try:
                    capture_browser_failure(
                        page, root / "debug-captures", attempt_id=attempt_id,
                        step=failed_step, configured_url=route.get("url"),
                        error=error, events=browser_events,
                    )
                except Exception:
                    # Diagnostics must never alter application state or shadow
                    # the original browser failure.
                    pass
            browser_failure_handled = True

        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(str(root / "browser-profile"), headless=True,
                    args=["--disable-gpu"], timeout=_playwright_action_timeout_ms(check, 15000))
                try:
                    context.route("**/*", lambda route: route.continue_() if client.allowed(route.request.url) else route.abort())
                    page = context.new_page()
                    if debug_capture:
                        page.on("console", lambda message: append_console_error(
                            browser_events, message,
                            getattr(message, "location", {}).get("url", page.url)))
                        page.on("pageerror", lambda error: append_diagnostic_event(
                            browser_events, "pageerror", page.url, error))
                        page.on("requestfailed", lambda request: append_diagnostic_event(
                            browser_events, "requestfailed", request.url,
                            getattr(request, "failure", None) or "REQUEST_FAILED"))
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("NAVIGATE")
                    store.transition(application_id, [application["state"]], "FILLING", "NAVIGATE")
                    page.goto(route["url"], wait_until="domcontentloaded",
                              timeout=_playwright_action_timeout_ms(check, 15000))
                    check()
                    finish_step("COMPLETED")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("DETECT_FORM")
                    intervention_detected = page.locator("[data-intervention]").count() > 0
                    finish_step("COMPLETED")
                    if intervention_detected:
                        store.transition(application_id, ["FILLING"], "USER_INTERVENTION_REQUIRED", "CHALLENGE_DETECTED")
                        return store.application(application_id)
                    store.transition(application_id, ["FILLING"], "FILLING", "DETECT_FORM")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("UPLOAD_RESUME")
                    page.get_by_label("Resume", exact=True).set_input_files(files[("resume", "pdf")])
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Cover letter", exact=True).set_input_files(files[("cover-letter", "pdf")])
                    finish_step("COMPLETED")
                    store.transition(application_id, ["FILLING"], "FILLING", "UPLOAD_RESUME")
                    check()
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("FILL_CONTACT")
                    page.get_by_label("Name", exact=True).fill(profile["name"])
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Email", exact=True).fill(profile["email"])
                    finish_step("COMPLETED")
                    store.transition(application_id, ["FILLING"], "FILLING", "FILL_CONTACT")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("FILL_LOCATION")
                    page.get_by_role("combobox", name="Country", exact=True).select_option(label=answer["country"])
                    finish_step("COMPLETED")
                    start_step("ANSWER_STANDARD")
                    if answer["remote"] != "yes":
                        raise ValueError("Unresolved mandatory remote answer")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Remote role").check()
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Documents reviewed").check()
                    finish_step("COMPLETED")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    if not page.locator("form").evaluate("form => form.checkValidity()"):
                        raise ValueError("Mock form validation failed")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_role("button", name="Review application", exact=True).click()
                    check()
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.locator('[data-validated="true"]').wait_for()
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    start_step("ANSWER_CUSTOM")
                    page.get_by_label("Application ID", exact=True).fill(application_id)
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Approved fixture answer", exact=True).fill(answer["fixture_question"])
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_label("Confirm fictional test submission").check()
                    finish_step("COMPLETED")
                    start_step("VALIDATE")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    if not page.locator("form").evaluate("form => form.checkValidity()"):
                        raise ValueError("Mock review validation failed")
                    finish_step("COMPLETED")
                    store.transition(application_id, ["FILLING"], "READY_TO_SUBMIT", "VALIDATE")
                    if not submit:
                        store.transition(application_id, ["READY_TO_SUBMIT"], "DRY_RUN_COMPLETE", "STOP_BEFORE_SUBMIT")
                        return store.application(application_id)
                    decision = _authorize(store, application_id, min(fact.confidence for fact in (*facts, *answers)), check=check)
                    if not decision.allowed:
                        store.transition(application_id, ["READY_TO_SUBMIT"], "REVIEW_REQUIRED", "POLICY_GATE")
                        return store.application(application_id)
                    # The operation deadline may expire after the durable
                    # reservation commits but before the irreversible submit
                    # click. Reconcile conservatively without clicking.
                    start_step("SUBMIT")
                    check()
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    page.get_by_role("button", name="Submit mock application", exact=True).click()
                    record_mock_application_submitted(store, application_id)
                    finish_step("COMPLETED")
                    start_step("VERIFY_CONFIRMATION")
                    page.set_default_timeout(_playwright_action_timeout_ms(check))
                    confirmation = page.locator("[data-confirmation]")
                    confirmation.wait_for(
                        timeout=_playwright_action_timeout_ms(check))
                    receipt = _receipt(
                        client, application, route["requisition"],
                        observed_url=page.url,
                        observed_text=" ".join(page.locator("body").inner_text().split()),
                        observed_reference=confirmation.get_attribute("data-confirmation"))
                    if receipt is None:
                        raise ValueError("Mock receipt missing or invalid after submission")
                    store.transition(application_id, ["SUBMITTING"], "CONFIRMED", "VERIFY_CONFIRMATION", receipt)
                    finish_step("COMPLETED")
                    return store.application(application_id)
                except Exception as error:
                    handle_browser_failure(error)
                    raise
                finally:
                    context.close()
        except Exception as error:
            if not browser_failure_handled:
                handle_browser_failure(error)
            raise
        finally:
            completed = store.application(application_id)
            with store.engine.begin() as connection:
                connection.execute(update(m.attempts).where(
                    m.attempts.c.id == attempt_id).values(state=completed["state"]))
    finally:
        client.close()
