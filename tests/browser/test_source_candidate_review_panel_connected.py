"""Connected Chromium acceptance for the profile-scoped source review panel."""
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.source_candidates.catalogue import report_source_candidate, review_candidate
from tests.integration.test_spine import PROFILE


class SourceProbe(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SourceCandidateReviewPanelConnectedTests(unittest.TestCase):
    def test_filters_review_retry_profile_isolation_and_durable_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-review-panel-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'source-review.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})

            SourceProbe.requests = []
            source_probe = HTTPServer(("127.0.0.1", 0), SourceProbe)
            probe_thread = threading.Thread(
                target=source_probe.serve_forever, daemon=True,
            )
            probe_thread.start()
            source_url = (
                f"http://127.0.0.1:{source_probe.server_port}"
                "/linkedin/jobs/view/517?tracking=private-token"
            )
            application_url = (
                "https://www.talenthop.com/apply/role-517?session=private-token#form"
            )

            def record(profile_id, request_id, domain, *, origin="LinkedIn",
                       source=None, destination=None):
                return report_source_candidate(
                    store,
                    profile_id=profile_id,
                    request_id=request_id,
                    discovery_origin=origin,
                    source_url=source or f"https://jobs.{domain}/posting/{request_id}",
                    application_url=destination or f"https://{domain}/apply/role/{request_id}",
                    observed_hops=[],
                )["candidate"]

            talenthop = record(
                "fixture", "review-panel-talenthop", "talenthop.com",
                source=source_url, destination=application_url,
            )
            alpha = record("fixture", "review-panel-alpha", "alpha.example")
            blocked = record("fixture", "review-panel-blocked", "bravo.example")
            other = record("other", "review-panel-other", "other-only.example",
                           origin="OtherProfileOrigin")

            review_candidate(
                store, "fixture", blocked["id"], "BLOCKED", "UNKNOWN",
                "Synthetic source remains unverified.", "review-panel-seed-blocked",
                "UNKNOWN", None,
                "No public feed evidence was supplied.", "Terms remain unknown.",
                "Capabilities remain unknown.", "Value and maintenance remain unknown.",
                None,
            )
            # Fixed timestamps make the explicit queue ordering deterministic.
            with store.engine.begin() as connection:
                for candidate, stamp in (
                    (talenthop, "2026-10-08T14:00:00+00:00"),
                    (alpha, "2026-10-08T13:00:00+00:00"),
                    (blocked, "2026-10-08T12:00:00+00:00"),
                    (other, "2026-10-08T15:00:00+00:00"),
                ):
                    connection.execute(update(m.source_candidates).where(
                        m.source_candidates.c.id == candidate["id"],
                    ).values(first_seen_at=stamp, last_seen_at=stamp))

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            server_thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            outside_requests = []
            forbidden_posts = []
            list_reads = []
            failed_requests = []
            page_errors = []
            review_attempts = []

            def counts():
                with store.engine.connect() as connection:
                    return {
                        table.name: connection.execute(
                            select(func.count()).select_from(table)
                        ).scalar_one()
                        for table in (
                            m.jobs, m.postings, m.applications, m.operations,
                            m.job_sources, m.source_candidates,
                            m.source_candidate_observations,
                            m.source_candidate_reviews,
                        )
                    }

            try:
                server_thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not server_thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated source-candidate API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile', 'fixture')"
                    )

                    def local_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin_url).netloc:
                            outside_requests.append(request.url)
                            route.abort()
                            return
                        if request.method == "GET" and parsed.path == "/source-candidates":
                            list_reads.append(parse_qs(parsed.query).get("profile_id", [None])[0])
                        if request.method == "POST" and (
                            parsed.path.startswith("/applications")
                            or parsed.path.endswith("/collect")
                            or parsed.path.endswith("/run")
                        ):
                            forbidden_posts.append(parsed.path)
                            route.abort()
                            return
                        if request.method == "PUT" and parsed.path.endswith("/review"):
                            payload = request.post_data_json
                            review_attempts.append(payload)
                            response = route.fetch()
                            if len(review_attempts) == 1:
                                # Let the server commit, then lose the response to
                                # exercise the exact retry-after-uncertainty path.
                                route.abort("connectionreset")
                                return
                            route.fulfill(response=response)
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on("requestfailed", lambda request: failed_requests.append(
                        {"url": request.url, "failure": request.failure}
                    ))
                    response = page.goto(origin_url, wait_until="domcontentloaded")
                    self.assertIsNotNone(response)
                    self.assertEqual(response.status, 200)

                    page.get_by_role("button", name="Sources", exact=True).click()
                    queue = page.get_by_role("region", name="Source candidate review queue")
                    expect(queue).to_be_visible()
                    cards = queue.get_by_role("list", name="Source candidates to review").get_by_role("article")
                    expect(cards).to_have_count(3)
                    labels = [cards.nth(index).get_attribute("aria-label")
                              for index in range(cards.count())]
                    self.assertEqual(labels, [
                        "Source candidate alpha.example",
                        "Source candidate talenthop.com",
                        "Source candidate bravo.example",
                    ])
                    self.assertNotIn("other-only.example", " ".join(labels))
                    self.assertIn("fixture", list_reads)

                    state_filter = page.get_by_label("Filter by review state", exact=True)
                    state_filter.select_option("BLOCKED")
                    expect(cards).to_have_count(1)
                    expect(cards.first).to_have_attribute(
                        "aria-label", "Source candidate bravo.example",
                    )
                    page.get_by_role("button", name="Clear filters", exact=True).click()
                    search_filter = page.get_by_label(
                        "Filter by source name or domain", exact=True,
                    )
                    search_filter.fill("talenthop")
                    expect(cards).to_have_count(1)
                    target = cards.first
                    expect(target).to_have_attribute(
                        "aria-label", "Source candidate talenthop.com",
                    )
                    expect(target).to_contain_text("Discovery origin (user reported): LinkedIn")
                    expect(target).to_contain_text("Observed hops: Unknown hop")
                    expect(target).to_contain_text("Source URL: http://127.0.0.1")
                    expect(target).to_contain_text("Application destination: https://www.talenthop.com/apply/role-517")
                    expect(target).not_to_contain_text("private-token")
                    expect(target).to_contain_text("Collection is disabled. Review does not enable collection.")

                    review = {
                        "site_type": "ATS",
                        "reason": "Synthetic 403 evidence; review only, no access attempt.",
                        "api_feed_evidence_status": "BLOCKED",
                        "api_feed_evidence_reference": "",
                        "api_feed_evidence_note": "A prior synthetic report supplied HTTP 403 evidence.",
                        "access_terms_note": "Authorization remains unverified.",
                        "capabilities_limits_note": "No source collection is authorized.",
                        "expected_value_maintenance_note": "Suitability remains unverified.",
                    }
                    target.get_by_label("Site type assessment for talenthop.com").select_option("ATS")
                    target.get_by_label("Review reason for talenthop.com", exact=True).fill(review["reason"])
                    target.get_by_label("API or feed evidence status for talenthop.com").select_option("BLOCKED")
                    target.get_by_label("API or feed evidence reference for talenthop.com").fill("")
                    target.get_by_label("API or feed evidence note for talenthop.com").fill(review["api_feed_evidence_note"])
                    target.get_by_label("Access and terms note for talenthop.com").fill(review["access_terms_note"])
                    target.get_by_label("Capabilities and limits note for talenthop.com").fill(review["capabilities_limits_note"])
                    target.get_by_label("Expected value and maintenance note for talenthop.com").fill(review["expected_value_maintenance_note"])

                    queue_button = target.get_by_role(
                        "button", name="Queue integration review", exact=True)
                    issue_number = target.get_by_label(
                        "Integration task issue number for talenthop.com", exact=True)
                    self.assertIn("required to queue integration", target.inner_text())
                    expect(queue_button).to_be_disabled()
                    issue_number.fill("302")
                    expect(queue_button).to_be_enabled()
                    issue_number.fill("")
                    expect(queue_button).to_be_disabled()

                    before_review = counts()
                    with page.expect_event(
                        "requestfailed",
                        predicate=lambda request: request.method == "PUT"
                        and request.url.endswith("/review"),
                    ):
                        target.get_by_role("button", name="Mark blocked", exact=True).click()
                    expect(target.get_by_role("alert")).to_be_visible()
                    expect(target.get_by_role("button", name="Retry review", exact=True)).to_be_visible()
                    self.assertEqual(counts()[m.source_candidate_reviews.name],
                                     before_review[m.source_candidate_reviews.name] + 1)

                    with page.expect_response(
                        lambda item: item.request.method == "PUT"
                        and item.url.endswith("/review") and item.status == 200,
                    ) as review_response:
                        target.get_by_role("button", name="Retry review", exact=True).click()
                    self.assertEqual(review_response.value.status, 200)
                    self.assertEqual(len(review_attempts), 2)
                    self.assertEqual(review_attempts[0], review_attempts[1])
                    self.assertEqual(review_attempts[0]["state"], "BLOCKED")
                    self.assertEqual(review_response.value.json()["id"], talenthop["id"])
                    expect(target).to_contain_text("Review history (1)")
                    expect(target).to_contain_text(review["reason"])

                    after_review = counts()
                    self.assertEqual(after_review[m.source_candidate_reviews.name],
                                     before_review[m.source_candidate_reviews.name] + 1)
                    for table in (m.jobs, m.postings, m.applications, m.operations,
                                  m.job_sources, m.source_candidates,
                                  m.source_candidate_observations):
                        self.assertEqual(after_review[table.name], before_review[table.name])
                    with store.engine.connect() as connection:
                        saved = connection.execute(select(m.source_candidates).where(
                            m.source_candidates.c.id == talenthop["id"],
                        )).mappings().one()
                        self.assertFalse(saved["collection_enabled"])

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    queue = page.get_by_role("region", name="Source candidate review queue")
                    cards = queue.get_by_role("list", name="Source candidates to review").get_by_role("article")
                    expect(cards).to_have_count(3)
                    target = queue.get_by_role("article", name="Source candidate talenthop.com")
                    expect(target).to_contain_text("Review history (1)")
                    expect(target).to_contain_text("BLOCKED · ATS · BLOCKED")
                    expect(target).to_contain_text("Synthetic 403 evidence; review only, no access attempt.")
                    expect(target).not_to_contain_text("private-token")

                    page.locator("#active-profile").select_option("other")
                    expect(page.get_by_role("article", name="Source candidate other-only.example")).to_be_visible()
                    expect(queue.get_by_role("list", name="Source candidates to review").get_by_role("article")).to_have_count(1)
                    expect(queue).not_to_contain_text("talenthop.com")
                    expect(queue).to_contain_text("OtherProfileOrigin")
                    self.assertIn("other", list_reads)

                    page.locator("#active-profile").select_option("fixture")
                    target = page.get_by_role("article", name="Source candidate talenthop.com")
                    expect(target).to_be_visible()
                    expect(target).to_contain_text("Review history (1)")
                    expect(page.get_by_role("article", name="Source candidate other-only.example")).to_have_count(0)

                    queued = page.get_by_role("article", name="Source candidate alpha.example")
                    queued.get_by_label("Site type assessment for alpha.example").select_option("JOB_BOARD")
                    queued.get_by_label("Review reason for alpha.example", exact=True).fill(
                        "Existing provider issue tracks review of this public job board.")
                    queued.get_by_label("API or feed evidence status for alpha.example").select_option("UNKNOWN")
                    queued.get_by_label("API or feed evidence note for alpha.example").fill(
                        "Public API or feed support is not verified.")
                    queued.get_by_label("Access and terms note for alpha.example").fill(
                        "Access terms need review before collection.")
                    queued.get_by_label("Capabilities and limits note for alpha.example").fill(
                        "No collection capability is currently enabled.")
                    queued.get_by_label("Expected value and maintenance note for alpha.example").fill(
                        "Source value and maintenance need bounded evaluation.")
                    queued.get_by_label("Integration task issue number for alpha.example").fill("318")
                    with page.expect_response(
                        lambda item: item.request.method == "PUT"
                        and item.url.endswith("/review") and item.status == 200,
                    ) as queue_response:
                        queued.get_by_role(
                            "button", name="Queue integration review", exact=True).click()
                    self.assertEqual(queue_response.value.json()["state"], "INTEGRATION_QUEUED")
                    self.assertEqual(queue_response.value.json()["integration_issue"], 318)
                    expect(queued).to_contain_text("Review state: INTEGRATION QUEUED")
                    expect(queued).to_contain_text("Integration issue #318 is user supplied and unverified.")
                    expect(queued).to_contain_text("Collection is disabled. Review does not enable collection.")

                    final_counts = counts()
                    self.assertEqual(final_counts[m.applications.name], 0)
                    self.assertEqual(final_counts[m.source_candidate_reviews.name],
                                     before_review[m.source_candidate_reviews.name] + 2)
                    self.assertEqual(final_counts[m.source_candidates.name], 4)
                    self.assertEqual(final_counts[m.source_candidate_observations.name], 4)
                    self.assertEqual(outside_requests, [])
                    self.assertEqual(forbidden_posts, [])
                    self.assertEqual(SourceProbe.requests, [])
                    self.assertEqual(page_errors, [])
                    unexpected_failures = [item for item in failed_requests
                                           if not (item["url"].endswith("/review")
                                                   and item["failure"])]
                    self.assertEqual(unexpected_failures, [])
                    browser.close()
            finally:
                server.should_exit = True
                server_thread.join(timeout=10)
                sock.close()
                source_probe.shutdown()
                source_probe.server_close()
                probe_thread.join(timeout=5)
                store.engine.dispose()
            self.assertFalse(server_thread.is_alive(), "Isolated API did not stop")
            self.assertFalse(probe_thread.is_alive(), "Synthetic source probe did not stop")


if __name__ == "__main__":
    unittest.main()
