"""Connected browser acceptance for the local-only application rehearsal."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
GREENHOUSE_URL = "https://boards.greenhouse.io/example/jobs/1234567"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationRehearsalBrowserTests(unittest.TestCase):
    def test_review_receipt_is_connected_local_and_readable_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-rehearsal-browser-") as directory:
            root = Path(directory)
            database = root / "rehearsal.db"
            database_url = f"sqlite:///{database.as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            application = store.application(application_id)
            safe_field_keys = ("candidate[first_name]", "field[]", "_custom")
            with store.engine.begin() as connection:
                connection.execute(m.routes.update().where(
                    m.routes.c.id == application["route_id"],
                ).values(provider="greenhouse", tenant="example", url=GREENHOUSE_URL))
                connection.execute(m.applications.update().where(
                    m.applications.c.id == application_id,
                ).values(target="REAL"))
                connection.execute(m.audit.insert().values(
                    id=identity("synthetic-form-review", application_id),
                    entity_id=application_id,
                    action="APPLICATION_FORM_REVIEW",
                    payload={
                        "operation_id": "synthetic-form-review-operation",
                        "request_id": "synthetic-form-review-request",
                        "input_fingerprint": "a" * 64,
                        "profile_id": "fixture",
                        "route_id": application["route_id"],
                        "provider": "greenhouse",
                        "analysis": {
                            "status": "ANALYZED",
                            "reason": None,
                            "fields": [
                                {"id": "email", "name": "email", "type": "email",
                                 "required": True, "review_reason": None},
                                {"id": "custom_question", "name": "custom_question",
                                 "type": "text", "required": True,
                                 "review_reason": "AMBIGUOUS_FIELD"},
                                *[
                                    {"id": key, "name": key, "type": "text",
                                     "required": True, "review_reason": "AMBIGUOUS_FIELD"}
                                    for key in safe_field_keys
                                ],
                            ],
                        },
                    },
                    timestamp=now(),
                ))

            def application_state():
                with store.engine.connect() as connection:
                    return {
                        "application": dict(connection.execute(m.applications.select().where(
                            m.applications.c.id == application_id,
                        )).mappings().one()),
                        "attempts": [dict(row) for row in connection.execute(
                            m.attempts.select().where(
                                m.attempts.c.application_id == application_id,
                            ).order_by(m.attempts.c.id),
                        ).mappings()],
                    }

            def review_receipts():
                with store.engine.connect() as connection:
                    return [dict(row) for row in connection.execute(m.audit.select().where(
                        m.audit.c.entity_id == application_id,
                        m.audit.c.action == "APPLICATION_FORM_REHEARSAL_REVIEWED",
                    ).order_by(m.audit.c.id)).mappings()]

            before_state = application_state()
            with store.engine.connect() as connection:
                profile_version = connection.execute(m.profiles.select().where(
                    m.profiles.c.id == "fixture",
                )).mappings().one()["version"]
            self.assertEqual(review_receipts(), [])
            rehearsal_path = f"/profiles/fixture/applications/{application_id}/rehearsal"
            review_path = rehearsal_path + "/reviews"
            self.rehearsal_evidence = {
                "schema_version": 1,
                "safe_field_keys": list(safe_field_keys),
                "phases": [],
            }

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated rehearsal API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []
                    writes = []
                    rehearsal_get_statuses = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                                writes.append({"method": route.request.method,
                                               "path": urlsplit(route.request.url).path})
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)

                    def observe_response(response):
                        if (response.request.method == "GET"
                                and urlsplit(response.url).path == rehearsal_path):
                            rehearsal_get_statuses.append(response.status)

                    page.on("response", observe_response)

                    def assert_safe_fields_visible(panel):
                        rows = panel.get_by_role("list", name="Rehearsal field values").get_by_role(
                            "listitem",
                        )
                        for key in safe_field_keys:
                            row = rows.filter(has=page.get_by_role("heading", name=key, exact=True))
                            expect(row).to_have_count(1)
                            expect(row.get_by_label(key + " rehearsal value", exact=True)).to_have_text(
                                "No value populated",
                            )
                            expect(row).to_contain_text("Unresolved")
                        blockers = panel.get_by_label("Rehearsal blockers", exact=True)
                        expect(blockers).to_contain_text("One or more form fields need manual review.")
                        expect(blockers).to_contain_text(
                            "One or more required fields do not have a verified value.",
                        )
                        expect(panel).to_contain_text("External submission is disabled")

                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    card = board.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False,
                    )
                    expect(card).to_be_visible()
                    card.click()

                    panel = page.get_by_role("region", name="Local application rehearsal")
                    expect(panel).to_contain_text("External submission is disabled")
                    expect(panel.get_by_label("email rehearsal value")).to_have_text(PROFILE["email"])
                    expect(panel).to_contain_text("custom_question")
                    expect(panel).to_contain_text("Why this is unresolved")
                    assert_safe_fields_visible(panel)
                    opened_state = application_state()
                    self.rehearsal_evidence["phases"].append({
                        "phase": "opened",
                        "get_statuses": list(rehearsal_get_statuses),
                        "application_unchanged": opened_state["application"] == before_state["application"],
                        "attempts_unchanged": opened_state["attempts"] == before_state["attempts"],
                        "write_count": len(writes),
                    })
                    self.assertTrue(rehearsal_get_statuses)
                    self.assertEqual(set(rehearsal_get_statuses), {200})
                    self.assertEqual(opened_state, before_state)
                    self.assertEqual(review_receipts(), [])
                    self.assertEqual(writes, [])
                    checkbox = panel.get_by_label("I have reviewed these rehearsal values locally")
                    checkbox.check()
                    with page.expect_response(lambda response: (
                        response.request.method == "POST"
                        and urlsplit(response.url).path == review_path
                    )) as recorded:
                        panel.get_by_role("button", name="Record local review receipt").click()
                    self.assertEqual(recorded.value.status, 200)
                    review_request = recorded.value.request.post_data_json
                    self.assertIsInstance(review_request, dict)
                    self.assertEqual(set(review_request), {"request_id", "fingerprint", "acknowledged"})
                    self.assertIs(review_request["acknowledged"], True)
                    expect(panel.get_by_role("status").filter(
                        has_text="Review receipt recorded locally",
                    )).to_be_visible()
                    expect(panel).not_to_contain_text("Submitted")
                    assert_safe_fields_visible(panel)
                    receipts = review_receipts()
                    reviewed_state = application_state()
                    self.rehearsal_evidence["phases"].append({
                        "phase": "acknowledged",
                        "receipt_count": len(receipts),
                        "request_keys": sorted(review_request),
                        "application_unchanged": reviewed_state["application"] == before_state["application"],
                        "attempts_unchanged": reviewed_state["attempts"] == before_state["attempts"],
                        "writes": list(writes),
                    })
                    self.assertEqual(len(receipts), 1)
                    expected_payload = {
                        "receipt_version": 1, "profile_id": "fixture", "application_id": application_id,
                        "request_id": review_request["request_id"], "fingerprint": review_request["fingerprint"],
                        "profile_version": profile_version, "route_id": application["route_id"],
                        "acknowledged": True,
                    }
                    self.assertEqual(receipts[0]["payload"], expected_payload)
                    self.assertEqual(reviewed_state, before_state)
                    self.assertEqual(writes, [{"method": "POST", "path": review_path}])
                    get_count_before_reopen = len(rehearsal_get_statuses)

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    board.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False,
                    ).click()
                    panel = page.get_by_role("region", name="Local application rehearsal")
                    expect(panel.get_by_role("status").filter(
                        has_text="Review receipt recorded locally",
                    )).to_be_visible()
                    assert_safe_fields_visible(panel)
                    reopened_state = application_state()
                    reopened_receipts = review_receipts()
                    self.rehearsal_evidence["phases"].append({
                        "phase": "reopened",
                        "new_rehearsal_get": len(rehearsal_get_statuses) > get_count_before_reopen,
                        "get_statuses": list(rehearsal_get_statuses),
                        "application_unchanged": reopened_state["application"] == before_state["application"],
                        "attempts_unchanged": reopened_state["attempts"] == before_state["attempts"],
                        "same_receipt": reopened_receipts == receipts,
                        "writes": list(writes),
                        "external_request_count": len(external),
                    })
                    self.assertGreater(len(rehearsal_get_statuses), get_count_before_reopen)
                    self.assertEqual(set(rehearsal_get_statuses), {200})
                    self.assertEqual(reopened_state, before_state)
                    self.assertEqual(reopened_receipts, receipts)
                    self.assertEqual(writes, [{"method": "POST", "path": review_path}])
                    self.assertFalse(external, external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
