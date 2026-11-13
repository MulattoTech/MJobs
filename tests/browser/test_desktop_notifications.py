"""Mounted desktop alerts use actual Chromium Notifications and scoped API polling.

Permission is pregranted only at the browser boundary; the user-facing MJobs
opt-in is still required. A transparent constructor Proxy forwards to the native
Notification implementation and records handoff/show/error events. These tests
do not certify a visible operating-system toast or background/closed-page delivery.
"""
from contextlib import contextmanager
import json
import os
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


_NATIVE_OBSERVER = """
(() => {
  if (!localStorage.getItem('mjobs-profile')) {
    localStorage.setItem('mjobs-profile', 'fixture');
  }
  const NativeNotification = window.Notification;
  const evidence = {constructed: [], pending: []};
  window.__desktopEvidence = evidence;
  function record(value) {
    evidence.pending.push(window.__recordDesktopEvidence(value));
  }
  window.Notification = new Proxy(NativeNotification, {
    construct(target, args) {
      const notice = Reflect.construct(target, args, target);
      const event = {
        phase: 'construct', title: notice.title, body: notice.body,
        tag: notice.tag, silent: notice.silent, renotify: notice.renotify,
        native: notice instanceof NativeNotification,
      };
      evidence.constructed.push(event);
      record(event);
      for (const phase of ['show', 'error', 'close']) {
        notice.addEventListener(phase, () => record({phase, tag: notice.tag}));
      }
      return notice;
    },
    get(target, key) {
      const value = Reflect.get(target, key, target);
      return key === 'requestPermission' ? value.bind(target) : value;
    },
  });
})();
"""


def _complete(store, operation, state="COMPLETED"):
    with store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
            state=state, updated_at=now(), error="PRIVATE_OPERATION_ERROR"))


@contextmanager
def _desktop_app(tmp_path, *, enabled=True):
    store = Store(f"sqlite:///{(tmp_path / 'desktop-notifications.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("PERSONAL")
    store.save_profile(PROFILE)
    store.save_profile({**PROFILE, "id": "other", "name": "PRIVATE_OTHER_PROFILE"})
    with patch.dict(os.environ, {"MJOBS_NOTIFICATIONS_ENABLED": "1" if enabled else "0"}):
        app = create_app(store, "http://127.0.0.1:1", tmp_path,
                         "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
    sock = bound_browser_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Owned desktop API failed to start"
            time.sleep(.02)
        yield store, origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        store.engine.dispose()
        assert not thread.is_alive(), "Owned desktop API did not stop"


def _operation_response(response, profile_id, *, status=200):
    parsed = urlsplit(response.url)
    return (response.request.method == "GET" and parsed.path == "/operations"
            and parse_qs(parsed.query).get("profile_id") == [profile_id]
            and response.status == status)


def _settle_delivery(page):
    # Let the mounted React consumer observe the completed read, then await the
    # transparent observer's callbacks. No application timer or clock is replaced.
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.evaluate("() => Promise.all(window.__desktopEvidence.pending)")


def _next_poll(page, profile_id, *, status=200):
    with page.expect_response(
            lambda response: _operation_response(response, profile_id, status=status),
            timeout=22000) as pending:
        pass
    response = pending.value
    response.finished()
    _settle_delivery(page)
    return response


def _constructs(events):
    return [event for event in events if event["phase"] == "construct"]


@contextmanager
def _mounted_browser(origin):
    external, writes, errors, native_events = [], [], [], []
    fault = {"profile": None, "remaining": 0}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.grant_permissions(["notifications"], origin=origin)
            context.expose_binding(
                "__recordDesktopEvidence", lambda _source, event: native_events.append(event))
            context.add_init_script(_NATIVE_OBSERVER)

            def local_only(route):
                parsed = urlsplit(route.request.url)
                if parsed.netloc != urlsplit(origin).netloc:
                    external.append(route.request.url)
                    route.abort()
                elif (fault["remaining"] and parsed.path == "/operations"
                      and parse_qs(parsed.query).get("profile_id") == [fault["profile"]]):
                    fault["remaining"] -= 1
                    route.fulfill(status=503, content_type="application/json",
                                  body='{"detail":"SYNTHETIC_ACTIVITY_UNAVAILABLE"}')
                else:
                    route.continue_()

            context.route("**/*", local_only)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: writes.append(request.url)
                    if request.method not in {"GET", "HEAD", "OPTIONS"} else None)
            page.goto(origin, wait_until="domcontentloaded")
            expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
            # Finish overlapping initial reads before targeting a later polling failure.
            page.wait_for_load_state("networkidle")
            assert page.evaluate(
                "() => isSecureContext && typeof Notification === 'function' "
                "&& Notification.permission === 'granted' && !!navigator.locks")
            yield page, native_events, fault
            _settle_delivery(page)
            print("Desktop native API evidence: " + json.dumps({
                "constructor_handoffs": _constructs(native_events),
                "observed_events": [event["phase"] for event in native_events
                                    if event["phase"] != "construct"],
                "os_visible_toast_verified": False,
            }, sort_keys=True))
        finally:
            browser.close()
    assert external == [] and writes == [] and errors == []


def test_desktop_provider_respects_global_notification_configuration(tmp_path):
    with _desktop_app(tmp_path, enabled=False) as (store, origin):
        historical = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "desktop-history"})
        _complete(store, historical)
        with _mounted_browser(origin) as (page, events, _fault):
            expect(page.get_by_role("heading", name="Find your next opportunity.")).to_be_visible()
            expect(page.get_by_role("region", name="Desktop notifications", exact=True)).to_have_count(0)
            assert _constructs(events) == []


def test_native_desktop_polling_opt_in_deduplication_disable_and_profile_isolation(tmp_path):
    with _desktop_app(tmp_path) as (store, origin):
        historical = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "desktop-history"})
        _complete(store, historical)
        current = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "desktop-current"})
        disabled_event = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "desktop-disabled"})
        foreign = enqueue_operation(store, "DISCOVER", "other", {"request_id": "desktop-foreign"})
        with _mounted_browser(origin) as (page, events, _fault):
            panel = page.get_by_role("region", name="Desktop notifications", exact=True)
            activity = page.get_by_role("region", name="Activity notifications", exact=True)
            expect(panel).to_contain_text("Desktop notifications are off for this profile.")
            expect(activity).to_contain_text("Job discovery completed.")
            _settle_delivery(page)
            # Browser permission alone never opts a profile into desktop alerts.
            assert _constructs(events) == []
            panel.get_by_role("button", name="Enable desktop notifications", exact=True).click()
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _settle_delivery(page)
            assert _constructs(events) == [], "Existing terminal history was replayed on opt-in"

            _complete(store, foreign, "FAILED")
            _complete(store, current)
            # No refresh/button action: the application's real 15-second poll
            # reads the owned API and forwards this new outcome to Chromium.
            page.wait_for_function(
                "() => window.__desktopEvidence.constructed.length === 1", timeout=22000)
            _settle_delivery(page)
            first = _constructs(events)
            assert len(first) == 1
            assert first[0]["title"] == "MJobs activity"
            assert first[0]["body"] == "Job discovery completed."
            assert first[0]["native"] is True
            assert first[0]["silent"] is True and first[0]["renotify"] is False
            assert first[0]["tag"] and "PRIVATE" not in json.dumps(first)

            _next_poll(page, "fixture")
            assert len(_constructs(events)) == 1, "An unchanged poll repeated the native alert"

            page.reload(wait_until="domcontentloaded")
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _next_poll(page, "fixture")
            assert len(_constructs(events)) == 1, "Reload replayed a previously handed-off event"

            panel.get_by_role("button", name="Disable desktop notifications", exact=True).click()
            expect(panel).to_contain_text("Desktop notifications are off for this profile.")
            _complete(store, disabled_event, "FAILED")
            expect(activity).to_contain_text("Job discovery needs attention.", timeout=22000)
            _settle_delivery(page)
            assert len(_constructs(events)) == 1, "Disabled profile emitted a desktop alert"
            panel.get_by_role("button", name="Enable desktop notifications", exact=True).click()
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _settle_delivery(page)
            assert len(_constructs(events)) == 1, "Re-enabling replayed the disabled interval"

            page.get_by_label("Active profile", exact=True).select_option("other")
            expect(panel).to_contain_text("Desktop notifications are off for this profile.")
            expect(activity).to_contain_text("Job discovery needs attention.")
            _settle_delivery(page)
            assert len(_constructs(events)) == 1, "Profile opt-in leaked to a different profile"
            panel.get_by_role("button", name="Enable desktop notifications", exact=True).click()
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _settle_delivery(page)
            assert len(_constructs(events)) == 1, "Other-profile history was replayed on opt-in"

            other_current = enqueue_operation(store, "PREPARE", "other", {})
            _complete(store, other_current)
            page.wait_for_function(
                "() => window.__desktopEvidence.constructed.length === 1", timeout=22000)
            _settle_delivery(page)
            delivered = _constructs(events)
            assert len(delivered) == 2
            assert delivered[-1]["body"] == "Document preparation completed."
            assert delivered[-1]["native"] is True
            assert delivered[0]["tag"] != delivered[-1]["tag"]
            assert "PRIVATE" not in json.dumps(delivered)
            page.get_by_label("Active profile", exact=True).select_option("fixture")
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _settle_delivery(page)
            assert len(_constructs(events)) == 2


def test_desktop_activity_failure_pauses_and_recovery_baselines_without_replay(tmp_path):
    with _desktop_app(tmp_path) as (store, origin):
        during_failure = enqueue_operation(store, "DISCOVER", "fixture", {})
        with _mounted_browser(origin) as (page, events, fault):
            panel = page.get_by_role("region", name="Desktop notifications", exact=True)
            panel.get_by_role("button", name="Enable desktop notifications", exact=True).click()
            expect(panel.get_by_role("button", name="Disable desktop notifications", exact=True)).to_be_visible()
            _settle_delivery(page)
            fault.update(profile="fixture", remaining=1)
            with page.expect_response(
                    lambda response: _operation_response(response, "fixture", status=503),
                    timeout=22000):
                _complete(store, during_failure)
            expect(panel).to_contain_text("paused while activity updates are unavailable")
            _settle_delivery(page)
            assert _constructs(events) == []

            _next_poll(page, "fixture")
            expect(panel).not_to_contain_text("paused while activity updates are unavailable")
            _settle_delivery(page)
            assert _constructs(events) == [], "Recovery replayed an uncertain outage interval"
            after_recovery = enqueue_operation(store, "PREPARE", "fixture", {})
            _complete(store, after_recovery)
            page.wait_for_function(
                "() => window.__desktopEvidence.constructed.length === 1", timeout=22000)
            _settle_delivery(page)
            delivered = _constructs(events)
            assert len(delivered) == 1
            assert delivered[0]["body"] == "Document preparation completed."
            assert delivered[0]["native"] is True
