"""Real ATS adapter methods on static owned pages, without submit authority."""
import hashlib
import json
import threading
import time
from copy import deepcopy
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.owned_ats_fixture import OwnedATSFixtureError, freeze_fixture
from tests.unit.test_owned_ats_fixture import fixture_inputs, forwarding_inputs

HTML = """<!doctype html><html><body><form>
<input name="email" type="email" aria-label="Email" required>
<input name="resume" type="file" aria-label="Resume" required>
<input name="cover_letter" type="file" aria-label="Cover letter" required>
</form></body></html>"""


@pytest.fixture(params=["greenhouse", "lever"])
def owned_page(request):
    provider = request.param
    inputs = fixture_inputs(provider)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            attempted_requests = []

            def owned_only(route):
                attempted_requests.append((route.request.method, route.request.url))
                if route.request.url == inputs["page_url"] and route.request.method == "GET":
                    route.fulfill(status=200, content_type="text/html", body=HTML,
                                  headers={"Content-Security-Policy": "default-src 'none'; form-action 'none'"})
                else:
                    route.abort()

            context.route("**/*", owned_only)
            page = context.new_page()
            page.goto(inputs["page_url"], wait_until="domcontentloaded")
            adapter = GreenhouseApplicationAdapter() if provider == "greenhouse" else LeverApplicationAdapter()
            inputs.update(template=page.content(), analysis=adapter.analyze_page(page))
            yield page, inputs, attempted_requests, adapter
            assert attempted_requests == [("GET", inputs["page_url"])]
        finally:
            browser.close()


def test_native_validation_blocks_upload_then_actual_adapter_selects_exact_bytes(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, adapter = owned_page
    contract = freeze_fixture(**inputs)
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ADAPTER_REVIEW_REQUIRED"):
        upload_fixture_documents(page, contract, current=inputs)
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    result = upload_fixture_documents(page, contract, current=inputs)
    assert result["verification"] == "BROWSER_SELECTED_BYTES"
    assert result["external_submission_enabled"] is False
    assert result["documents"] == [document.manifest() for document in inputs["documents"]]
    assert page.locator("form").evaluate("form => form.checkValidity()") is True
    assert adapter.capabilities()["submission"] is False
    assert adapter.capabilities()["live_form_interaction"] is False


@pytest.mark.parametrize("changed", ["profile", "documents", "template", "question"])
def test_stale_review_prevents_any_document_selection(owned_page, changed):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, _ = owned_page
    contract = freeze_fixture(**inputs)
    current = deepcopy(inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    if changed == "profile":
        current["profile"]["version"] += 1
    elif changed == "documents":
        doc = current["documents"][0]
        content = doc.content + b"changed"
        current["documents"][0] = replace(doc, content=content, sha256=hashlib.sha256(content).hexdigest())
    elif changed == "template":
        page.locator("form").evaluate("form => form.setAttribute('action', '/changed')")
    else:
        page.locator('[name="email"]').evaluate("node => node.setAttribute('aria-label', 'Another question')")
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        upload_fixture_documents(page, contract, current=current)
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]


def test_reviewed_custom_binding_preserves_actual_provider_review_policy(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, adapter = owned_page
    page.locator("form").evaluate("""form => {
      const field = document.createElement('textarea');
      field.name = 'availability'; field.setAttribute('aria-label', 'Which days are you available?');
      form.appendChild(field);
    }""")
    inputs.update(template=page.content(), analysis=adapter.analyze_page(page),
                  reviewed_fields=({"field_key": "availability", "answer_id": "availability-1"},))
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    page.locator('[name="availability"]').fill("Weekdays only.")
    if inputs["provider"] == "lever":
        with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ADAPTER_REVIEW_REQUIRED"):
            upload_fixture_documents(page, contract, current=inputs)
        assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    else:
        result = upload_fixture_documents(page, contract, current=inputs)
        assert len(result["documents"]) == 2


def test_extra_browser_page_stops_upload_before_selection(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, _ = owned_page
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    extra = page.context.new_page()
    try:
        with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
            upload_fixture_documents(page, contract, current=inputs)
        assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    finally:
        extra.close()


def add_custom_question(page, inputs, adapter):
    page.locator("form").evaluate("""form => {
      const field = document.createElement('textarea');
      field.name = 'availability'; field.required = true;
      field.setAttribute('aria-label', 'Which days are you available?');
      form.appendChild(field);
    }""")
    inputs.update(template=page.content(), analysis=adapter.analyze_page(page),
                  reviewed_fields=({"field_key": "availability", "answer_id": "availability-1"},))


def test_explicit_reviewed_consumer_fills_bound_answer_and_uses_actual_adapter_upload(owned_page):
    from services.applications.owned_ats_fixture import prepare_reviewed_fixture
    page, inputs, _, adapter = owned_page
    add_custom_question(page, inputs, adapter)
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_FIELD_REVIEW_REQUIRED"):
        freeze_fixture(**{**inputs, "reviewed_fields": ()})
    assert page.locator('[name="availability"]').input_value() == ""
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    receipt = prepare_reviewed_fixture(page, contract, current=inputs)
    assert page.locator('[name="availability"]').input_value() == "Weekdays only."
    assert receipt["documents"] == [item.manifest() for item in inputs["documents"]]
    assert receipt["reviewed_field_count"] == 1
    assert page.locator("form").evaluate("form => form.checkValidity()") is True
    # The public adapter retains its original review and capability policy.
    if inputs["provider"] == "lever":
        assert adapter.requires_human_review(inputs["analysis"]) is True
    assert adapter.capabilities()["submission"] is False


@pytest.mark.parametrize("changed", ["profile", "documents", "question", "template"])
def test_reviewed_consumer_stale_input_stops_before_answer_fill(owned_page, changed):
    from services.applications.owned_ats_fixture import prepare_reviewed_fixture
    page, inputs, _, adapter = owned_page
    add_custom_question(page, inputs, adapter)
    contract = freeze_fixture(**inputs)
    current = deepcopy(inputs)
    if changed == "profile":
        current["profile"]["answers"][0]["text"] = "A changed answer."
    elif changed == "documents":
        current["documents"][0] = replace(current["documents"][0], document_id="another-document")
    elif changed == "question":
        page.locator('[name="availability"]').evaluate("n => n.setAttribute('aria-label', 'Different question')")
    else:
        page.locator("form").evaluate("n => n.setAttribute('action', '/other')")
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        prepare_reviewed_fixture(page, contract, current=current)
    assert page.locator('[name="availability"]').input_value() == ""
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]


def test_reviewed_answer_readback_failure_prevents_upload(owned_page):
    from services.applications.owned_ats_fixture import prepare_reviewed_fixture
    page, inputs, _, adapter = owned_page
    add_custom_question(page, inputs, adapter)
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    page.locator('[name="availability"]').evaluate(
        "node => node.addEventListener('input', () => {node.value = 'changed';})")
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ANSWER_READBACK_MISMATCH"):
        prepare_reviewed_fixture(page, contract, current=inputs)
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]


def test_sensitive_custom_question_is_never_admitted_by_reviewed_consumer(owned_page):
    page, inputs, _, adapter = owned_page
    add_custom_question(page, inputs, adapter)
    page.locator('[name="availability"]').evaluate("n => n.setAttribute('aria-label', 'Social Security number')")
    inputs.update(template=page.content(), analysis=adapter.analyze_page(page))
    inputs["profile"]["answers"][0]["question"] = "Social Security number"
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_FORM_REVIEW_REQUIRED"):
        freeze_fixture(**inputs)
    assert page.locator('[name="availability"]').input_value() == ""
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]


FORWARDED_HTML = HTML.replace("<form>", '<form method="post" enctype="multipart/form-data" '
    'action="/submit/application-1/request-1">').replace("</form>",
    '<textarea name="availability" aria-label="Which days are you available?" required></textarea>'
    '<button type="submit">Submit owned fixture</button></form>')


@pytest.fixture
def upstream_fixture():
    state = {"requests": [], "receipts": {}, "document_hashes": {}}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type="text/html", **headers):
            content = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(content)
            except ConnectionError:
                pass  # The bounded timeout test deliberately closes its owned request.

        def do_GET(self):
            with lock:
                state["requests"].append(("GET", self.path))
            if self.path == "/form":
                self.reply(200, FORWARDED_HTML)
            elif self.path == "/receipt/application-1":
                self.reply(200, json.dumps(state["receipts"].get("application-1", {})), "application/json")
            elif self.path == "/redirect":
                self.reply(302, "", Location=state["origin"] + "/sink")
            elif self.path == "/external-redirect":
                self.reply(302, "", Location="https://example.invalid/never")
            elif self.path == "/drop":
                self.connection.close()
            elif self.path == "/slow":
                time.sleep(0.3)
                self.reply(200, FORWARDED_HTML)
            else:
                self.reply(404, "Not found")

        def do_POST(self):
            with lock:
                state["requests"].append(("POST", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            fields = {part.get_param("name", header="content-disposition"): part
                      for part in message.iter_parts()}
            if (self.path != "/submit/application-1/request-1"
                    or fields["availability"].get_payload(decode=True) != b"Weekdays only."):
                self.reply(422, "Invalid fixture")
                return
            with lock:
                state["document_hashes"] = {name: hashlib.sha256(fields[name].get_payload(decode=True)).hexdigest()
                                            for name in ("resume", "cover_letter")}
                state["receipts"].setdefault("application-1", {
                    "application_id": "application-1", "request_id": "request-1",
                    "reference_number": "MOCK-application-1", "submission_count": 1,
                    "confirmation_url": state["origin"] + self.path,
                    "document_hashes": dict(state["document_hashes"]),
                })
            if state.get("drop_submit_response"):
                self.connection.close()
                return
            self.reply(200, '<h1 data-confirmation="MOCK-application-1">Application received</h1>'
                       '<p>MOCK-application-1</p>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def forwarding_for_server(server):
    inputs = forwarding_inputs()
    inputs["upstream_origin"] = server["origin"]
    inputs["routes"] = (*inputs["routes"], ("GET", "/receipt/application-1"))
    return inputs


@pytest.mark.parametrize("provider", ["greenhouse", "lever"])
@pytest.mark.parametrize("lost_response", [False, True])
def test_facade_forwards_real_upload_and_confirmation_then_caller_recovers_without_resubmit(upstream_fixture, provider, lost_response):
    from services.applications.owned_ats_fixture import (
        freeze_forwarding,
        open_fixture_facade,
        prepare_reviewed_fixture,
    )
    forwarding = forwarding_for_server(upstream_fixture)
    frozen_transport = freeze_forwarding(**forwarding)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with open_fixture_facade(browser, frozen_transport, current=forwarding) as facade:
                page = facade.context.new_page()
                page.goto(forwarding["virtual_origin"] + "/form")
                inputs = fixture_inputs(provider)
                adapter = GreenhouseApplicationAdapter() if provider == "greenhouse" else LeverApplicationAdapter()
                inputs.update(origin=forwarding["virtual_origin"], page_url=page.url,
                              template=page.content(), analysis=adapter.analyze_page(page))
                with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_FIELD_REVIEW_REQUIRED"):
                    freeze_fixture(**inputs)
                assert upstream_fixture["receipts"] == {}
                assert not any(method == "POST" for method, _ in upstream_fixture["requests"])
                inputs["reviewed_fields"] = ({"field_key": "availability", "answer_id": "availability-1"},)
                contract = freeze_fixture(**inputs)
                page.locator('[name="email"]').fill(inputs["profile"]["email"])
                prepared = prepare_reviewed_fixture(page, contract, current=inputs, check=facade)
                assert prepared["external_submission_enabled"] is False
                # Explicit test-owned caller consent; the helper never clicks submit.
                if lost_response:
                    upstream_fixture["drop_submit_response"] = True
                    with page.expect_event("requestfailed", timeout=5000):
                        try:
                            page.get_by_role("button", name="Submit owned fixture").click(timeout=5000)
                        except PlaywrightError:
                            pass
                    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_FORWARDING_UNAVAILABLE"):
                        facade.assert_healthy()
                else:
                    page.get_by_role("button", name="Submit owned fixture").click()
                    expect(page.locator("[data-confirmation]")).to_have_attribute("data-confirmation", "MOCK-application-1")
                    facade.assert_healthy()
                receipt = upstream_fixture["receipts"]["application-1"]
                if not lost_response:
                    assert facade.upstream_url(page.url, method="POST") == receipt["confirmation_url"]
                assert receipt["document_hashes"] == {
                    "resume": inputs["documents"][0].sha256, "cover_letter": inputs["documents"][1].sha256}
            # Caller recovery reads the real server receipt before deciding whether to submit.
            with open_fixture_facade(browser, frozen_transport, current=forwarding) as recovered:
                page = recovered.context.new_page()
                response = page.goto(forwarding["virtual_origin"] + "/receipt/application-1")
                assert response.json() == receipt
                recovered.assert_healthy()
            assert upstream_fixture["requests"].count(("POST", "/submit/application-1/request-1")) == 1
            assert receipt["submission_count"] == 1
        finally:
            browser.close()


def test_facade_rechecks_current_origin_before_forwarding_any_request(upstream_fixture):
    from services.applications.owned_ats_fixture import (
        freeze_forwarding,
        open_fixture_facade,
    )
    forwarding = forwarding_for_server(upstream_fixture)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with open_fixture_facade(browser, freeze_forwarding(**forwarding), current=forwarding) as facade:
                forwarding["upstream_origin"] = "http://127.0.0.1:1"
                page = facade.context.new_page()
                with pytest.raises(PlaywrightError):
                    page.goto(forwarding["virtual_origin"] + "/form")
                with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_FORWARDING_STALE"):
                    facade.assert_healthy()
                assert upstream_fixture["requests"] == []
        finally:
            browser.close()


@pytest.mark.parametrize("scenario", ["wrong_origin", "wrong_path", "redirect", "external-redirect", "drop"])
def test_facade_blocks_unowned_requests_redirects_and_does_not_retry(upstream_fixture, scenario):
    from services.applications.owned_ats_fixture import (
        freeze_forwarding,
        open_fixture_facade,
    )
    forwarding = forwarding_for_server(upstream_fixture)
    forwarding["routes"] = (*forwarding["routes"], ("GET", "/redirect"),
                            ("GET", "/external-redirect"), ("GET", "/drop"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with open_fixture_facade(browser, freeze_forwarding(**forwarding), current=forwarding) as facade:
                page = facade.context.new_page()
                path = "/form" if scenario == "wrong_origin" else "/" + scenario
                origin = upstream_fixture["origin"] if scenario == "wrong_origin" else forwarding["virtual_origin"]
                with pytest.raises(PlaywrightError):
                    page.goto(origin + path)
                with pytest.raises(OwnedATSFixtureError):
                    facade.assert_healthy()
                assert upstream_fixture["receipts"] == {}
                expected = [] if scenario in {"wrong_origin", "wrong_path"} else [("GET", path)]
                assert upstream_fixture["requests"] == expected
        finally:
            browser.close()


def test_facade_preserves_operation_timeout_when_pending_upstream_request_times_out(upstream_fixture):
    from services.applications.owned_ats_fixture import (
        freeze_forwarding,
        open_fixture_facade,
    )
    from workers.operations import OperationTimedOut
    forwarding = forwarding_for_server(upstream_fixture)
    forwarding["routes"] = (*forwarding["routes"], ("GET", "/slow"))

    class Deadline:
        expires = None

        def remaining_seconds(self):
            return 5 if self.expires is None else self.expires - time.monotonic()

        def __call__(self):
            if self.remaining_seconds() <= 0:
                raise OperationTimedOut("owned test deadline")

    deadline = Deadline()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with open_fixture_facade(browser, freeze_forwarding(**forwarding), current=forwarding,
                                     check=deadline) as facade:
                page = facade.context.new_page()
                deadline.expires = time.monotonic() + 0.15
                with pytest.raises(PlaywrightError):
                    page.goto(forwarding["virtual_origin"] + "/slow")
                with pytest.raises(OperationTimedOut):
                    facade.assert_healthy()
                assert upstream_fixture["receipts"] == {}
                assert upstream_fixture["requests"] == [("GET", "/slow")]
        finally:
            browser.close()
