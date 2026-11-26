"""Controlled public-source collection is joined to the real local Jobs UI."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from apps.api.main import create_app
from cli.acceptance_journey import join_public_browser
from cli.acceptance_public_browser import _digest, _observe_job_list
from cli.acceptance_public_browser import run as observe_public_browser
from cli.acceptance_public_source import run as collect_public_jobs
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.discovery.sources.collect import _legacy_company_label
from services.discovery.sources.remotive import Remotive
from services.documents.generate import prepare
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]


class PublicJobListObservationTests(unittest.TestCase):
    def observe(self, *, scope="LIVE_PUBLIC_READ_ONLY", tied=False, wrong_order=False,
                extra_card=None, search_case=None, score_change=False, missing_baseline_score=False):
        origin = "http://127.0.0.1:18100"
        rows = [
            {"id": "public-a", "title": "Data Analyst", "match_score": 50},
            {"id": "public-b", "title": "Observability Engineer", "match_score": 50 if tied else 95},
            {"id": "public-c", "title": "Support Specialist", "match_score": None},
            {"id": "not-admitted", "title": "Data Engineer", "match_score": 100},
            {"id": "selected-fixture", "title": "Fixture Job", "match_score": 1},
        ]
        if search_case == "missing_title_hit":
            rows[1]["title"] = "Platform Analyst"
        elif search_case == "description_hit":
            rows[1]["description"] = "Works with an Analyst."
        job_cohort = {row["id"]: {"detail": row} for row in rows[:3]}
        document = """
            <label>Active profile<select id=profile aria-label='Active profile'><option>profile-a</option></select></label>
            <label>Job source<select id=source aria-label='Job source'><option value=''>All</option><option>source-a</option></select></label>
            <label>Search jobs<input id=q></label><label>Job title<input id=title></label>
            <label>Sort<select id=sort aria-label='Sort'><option>score</option><option>title</option></select></label>
            <div class=job-list></div>
            <script>
                const get = id => document.getElementById(id).value;
                async function refresh() {
                    const query = new URLSearchParams({profile_id: get('profile'), job_source_id: get('source'),
                        q: get('q'), title: get('title'), sort: get('sort')});
                    const {jobs, reverse_render, extra_card} = await (await fetch('/jobs?' + query)).json();
                    setTimeout(() => {
                        const list = document.querySelector('.job-list');
                        list.replaceChildren(...(reverse_render ? jobs.reverse() : jobs).map(job => {
                            const card = document.createElement('button');
                            card.className = 'job-card'; card.dataset.jobId = job.id;
                            const score = document.createElement('span');
                            score.className = 'score';
                            score.textContent = job.match_score === null ? 'Unscored' : job.match_score + '%';
                            card.append(score); return card;
                        }));
                        if (extra_card) {
                            const card = document.createElement('article');
                            card.className = 'job-card';
                            if (extra_card === 'duplicate_id') card.dataset.jobId = jobs[0].id;
                            card.textContent = 'Unexpected job card';
                            list.append(card);
                        }
                    }, 20);
                }
                document.querySelectorAll('select').forEach(control => control.onchange = refresh);
                document.querySelectorAll('input').forEach(control => control.oninput = refresh);
            </script>
        """

        def respond(route):
            request = route.request
            url = urlsplit(request.url)
            self.assertEqual(f"{url.scheme}://{url.netloc}", origin)
            self.assertEqual(request.method, "GET")
            if url.path == "/":
                route.fulfill(content_type="text/html", body=document)
                return
            self.assertEqual(url.path, "/jobs")
            query = parse_qs(url.query)
            self.assertEqual(query["profile_id"], ["profile-a"])
            job_source = query.get("job_source_id", [""])[0]
            self.assertIn(job_source, ("", "source-a"))
            terms = [query.get(key, [""])[0].lower() for key in ("q", "title")]
            q, title = terms
            matching = [dict(row) for row in rows if (not job_source or row["id"] != "selected-fixture")
                        and q in f'{row["title"]} {row.get("description", "")}'.lower()
                        and title in row["title"].lower()]
            if search_case == "missing_title_hit" and q:
                matching = [row for row in matching if row["id"] != "public-b"] if q == "analyst" else []
            by_score = query["sort"] == ["score"]
            if by_score and score_change:
                for row in matching:
                    if row["id"] == "public-a":
                        row["match_score"] = 65
            matching.sort(key=lambda row: ((-row["match_score"] if row["match_score"] is not None else 1)
                                          if by_score else 0, row["title"]))
            if not by_score and missing_baseline_score:
                for row in matching:
                    if row["id"] == "public-c":
                        del row["match_score"]
            route.fulfill(json={"jobs": matching, "reverse_render": wrong_order, "extra_card": extra_card})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", respond)
                page.goto(origin)
                return _observe_job_list(page, origin, {
                    "profile_id": "profile-a", "source_id": "source-a", "execution_scope": scope,
                }, job_cohort)
            finally:
                browser.close()

    def test_search_filter_and_sort_stay_bound_to_admitted_canonical_ids(self):
        evidence = self.observe()
        self.assertEqual(evidence["source_id"], "source-a")
        self.assertEqual(evidence["admitted_job_ids"], ["public-a", "public-b", "public-c"])
        self.assertEqual([row["status"] for row in evidence["observations"]], ["PASS"] * 3)
        search, title, sort = evidence["observations"]
        self.assertEqual(search["probe"]["visible_job_ids"], ["public-a"])
        self.assertEqual(search["probe"]["excluded_job_ids"], ["public-b", "public-c"])
        self.assertEqual(title["probe"]["visible_job_ids"], ["public-a"])
        self.assertEqual(title["probe"]["excluded_job_ids"], ["public-b", "public-c"])
        self.assertEqual(sort["title_sorted_jobs"], [
            {"job_id": "public-a", "match_score": 50},
            {"job_id": "public-b", "match_score": 95},
            {"job_id": "public-c", "match_score": None},
        ])
        self.assertEqual([row["job_id"] for row in sort["title_sorted_jobs"]],
                         evidence["source_scoped_job_ids"])
        self.assertEqual(sort["ranked_jobs"], [
            {"job_id": "public-b", "match_score": 95},
            {"job_id": "public-a", "match_score": 50},
            {"job_id": "public-c", "match_score": None},
        ])
        self.assertNotIn("not-admitted", json.dumps(evidence))
        self.assertNotIn("selected-fixture", json.dumps(evidence))

    def test_controlled_transport_never_claims_live_pass(self):
        receipt = self.observe(scope="CONTROLLED_TRANSPORT")
        self.assertEqual([row["status"] for row in receipt["observations"]], ["NOT_RUN"] * 3)
        self.assertTrue(all(all(row["checks"].values()) for row in receipt["observations"]))

    def test_only_tied_scores_cannot_prove_match_sort(self):
        receipt = self.observe(tied=True)
        self.assertEqual([row["status"] for row in receipt["observations"]], ["PASS", "PASS", "PARTIAL"])

    def test_mismatched_dom_order_cannot_be_used_as_evidence(self):
        with self.assertRaises(PlaywrightTimeout):
            self.observe(wrong_order=True)

    def test_all_job_cards_must_have_unique_ids(self):
        for extra_card in ("missing_id", "duplicate_id"):
            with self.subTest(extra_card=extra_card), self.assertRaises(PlaywrightTimeout):
                self.observe(extra_card=extra_card)

    def test_search_with_a_missing_title_match_cannot_pass(self):
        search, title, sort = self.observe(search_case="missing_title_hit")["observations"]
        self.assertEqual(search["status"], "PARTIAL")
        self.assertFalse(search["checks"]["matching_job_visible"])
        self.assertEqual((title["status"], sort["status"]), ("PASS", "PASS"))

    def test_search_permits_a_description_only_hit(self):
        search = self.observe(search_case="description_hit")["observations"][0]
        self.assertEqual(search["status"], "PASS")
        self.assertEqual(search["probe"]["visible_job_ids"], ["public-a", "public-b"])
        self.assertEqual(search["probe"]["excluded_job_ids"], ["public-c"])

    def test_sort_requires_unchanged_scores_between_independent_readbacks(self):
        sort = self.observe(score_change=True)["observations"][2]
        self.assertEqual(sort["status"], "PARTIAL")
        self.assertFalse(sort["checks"]["match_sort_order"])
        self.assertEqual(sort["title_sorted_jobs"][0], {"job_id": "public-a", "match_score": 50})
        self.assertIn({"job_id": "public-a", "match_score": 65}, sort["ranked_jobs"])

    def test_sort_rejects_a_missing_baseline_score_field(self):
        with self.assertRaisesRegex(ValueError, "^PUBLIC_BROWSER_MATCH_SCORE_INVALID$"):
            self.observe(missing_baseline_score=True)


class PublicBrowserListIsolationTests(unittest.TestCase):
    def run_browser(self, fault, *, employer=False):
        origin = "http://127.0.0.1:18100"
        endpoint = "https://remotive.com/api/remote-jobs"
        raw = RAW_REMOTIVE_PAYLOAD["jobs"][0]
        normalized = Remotive().normalize(raw)
        company, company_id = _legacy_company_label("remotive", raw)
        source_id, job_id = "b" * 64, "public-job"
        source_job_id = identity(company_id, normalized["requisition"])
        snapshot = {"id": "snapshot", "sha256": _digest(raw)}
        posting = {"id": identity(source_id, source_job_id), "source_job_id": source_job_id,
                   "job_id": job_id, "source": endpoint, "source_url": raw["url"],
                   "raw": raw, "fetched_at": "2026-01-01T00:00:00Z", "snapshots": [snapshot]}
        native_id = normalized["attributes"]["native_posting_id"]
        job = {"id": job_id, "title": normalized["title"], "company": company,
               "company_id": company_id, "location": raw["candidate_required_location"],
               "remote_type": "REMOTE_US", "provider": "remotive", "postings": [posting],
               "attributes": {"fictional": False, "native_posting_id": native_id,
                              "source_id": source_id, "provider": "remotive"}}
        baseline = {"source": {"commit": "a" * 40}, "profile_id": "profile-a",
                    "application_id": "application-a", "operation_id": "operation-a",
                    "request_id": "request-a", "selected_application_job_id": "fixture-job",
                    "selected_application_route_id": "route-a", "application_document_ids": [],
                    "target_origin": origin}
        public = {**baseline, "result": "PASS", "external_submission_enabled": False,
                  "timestamp": "2026-01-01T00:00:00Z", "acceptance_run_id": "synthetic-run",
                  "collection_operation_id": "collection-a", "collection_request_id": "collect-a",
                  "source_id": source_id, "endpoint": endpoint, "provider": "remotive",
                  "execution_scope": "LIVE_PUBLIC_READ_ONLY", "profile_version": "version-a",
                  "observed_real_job_ids": [job_id], "observations": [{
                      "job_id": job_id, "source_id": source_id, "source_endpoint": endpoint,
                      "fictional": False, "native_posting_id": native_id,
                      "postings": [{"posting_id": posting["id"], "raw_sha256": snapshot["sha256"],
                                    "fetched_at": posting["fetched_at"], "snapshots": [snapshot]}],
                  }]}
        if employer:
            public.update(employer_verification_requested=True, employer_observation={"status": "COMPLETE"})
        runtime = {"source_manifest_sha256": "c" * 64, "dockerfile_sha256": "d" * 64,
                   "images": {}, "frontend_assets": {}}
        api_payloads = {
            "/profiles/profile-a": {"id": "profile-a", "version": "version-a"},
            "/profiles/profile-a/applications/application-a": {
                "id": "application-a", "profile_id": "profile-a", "job_id": "fixture-job", "route_id": "route-a"},
            "/profiles/profile-a/applications/application-a/documents": {"documents": []},
            f"/jobs/{job_id}": job,
        }

        def api_response(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json=api_payloads[request.url.path])

        document = f"""
            <label>Active profile<select aria-label="Active profile"><option>profile-a</option></select></label>
            <nav><button id="jobs-tab">Jobs</button></nav>
            <label>Job source<select id="source" aria-label="Job source"><option value="">All</option>
                <option value="{source_id}">Source</option></select></label>
            <label>Search jobs<input></label><label>Job title<input></label>
            <label>Sort<select id="sort" aria-label="Sort"><option>score</option><option>title</option></select></label>
            <div class="job-list"><button class="job-card" data-job-id="{job_id}">
                <span class="eyebrow">{company}</span><span class="job-card-title">{job['title']}</span>
                <span role="group" aria-label="Job route provenance">remotive.com/api/remote-jobs
                    APPLICATION PROVIDER remotive</span></button></div>
            <section class="job-detail" hidden><span class="eyebrow">{company}</span><h2>{job['title']}</h2>
                {job['location']} {job['remote_type']}
                <div class="provenance">{endpoint}<a href="{raw['url']}">Source</a> SHA256 {snapshot['sha256']}</div>
            </section>
            <script>
                document.querySelector('.job-card').onclick = async () => {{
                    await fetch('/jobs/{job_id}?profile_id=profile-a');
                    document.querySelector('.job-detail').hidden = false;
                }};
            </script>
        """
        pages = []
        later_pages = []

        def browser_response(route):
            request = route.request
            self.assertEqual(request.method, "GET")
            url = urlsplit(request.url)
            if url.path == "/":
                script = ""
                if len(pages) == 2:
                    if fault in {"selector", "egress"}:
                        script = "document.querySelector('#sort').value = 'title';"
                    if fault == "egress":
                        script += "fetch('https://blocked.invalid/probe').catch(() => {});"
                    if fault == "integrity":
                        script += f"""document.querySelector('#source').onchange = () =>
                            fetch('/jobs?profile_id=profile-a&job_source_id={source_id}&sort=score');"""
                route.fulfill(content_type="text/html", body=document + f"<script>{script}</script>")
            elif url.path == "/jobs":
                route.fulfill(json={"jobs": [{"id": job_id}, {"id": job_id}]})
            else:
                route.fulfill(json=api_payloads[url.path])

        def observe_later(page, *_args):
            self.assertIs(page, pages[0])
            self.assertTrue(pages[1].is_closed())
            self.assertTrue(page.locator("section.job-detail").is_visible())
            self.assertEqual(page.get_by_label("Sort").input_value(), "score")
            later_pages.append(page)
            return {"synthetic_later_segment": True}

        api = httpx.Client(base_url=origin, transport=httpx.MockTransport(api_response))
        with sync_playwright() as playwright:
            chromium = playwright.chromium
            launch = chromium.launch

            def launch_routed(**kwargs):
                browser = launch(**kwargs)
                new_context = browser.new_context

                def create_context(**options):
                    context = new_context(**options)
                    install_route, new_page = context.route, context.new_page

                    def route_with_synthetic_loopback(pattern, handler):
                        install_route(pattern, handler)
                        install_route(origin + "/**", browser_response)

                    def track_page():
                        page = new_page()
                        pages.append(page)
                        return page

                    context.route = route_with_synthetic_loopback
                    context.new_page = track_page
                    return context

                browser.new_context = create_context
                return browser

            with (patch.object(chromium, "launch", launch_routed),
                  patch("cli.acceptance_public_browser.sync_playwright") as browser_context,
                  patch("cli.acceptance_public_browser.httpx.Client", return_value=api),
                  patch("cli.ui_e2e.validate_employer_verification_witness"),
                  patch("cli.acceptance_public_employer.observe_browser", side_effect=observe_later)):
                browser_context.return_value.__enter__.return_value = playwright
                evidence = observe_public_browser(origin, baseline, public, runtime_identity=runtime)
        self.assertEqual(len(later_pages), int(employer))
        return evidence, baseline, public, runtime

    def test_list_selector_failure_keeps_public_proof_and_runs_later_employer_segment(self):
        evidence, *_ = self.run_browser("selector", employer=True)
        self.assertNotIn("job_list_observation", evidence)
        self.assertEqual([(row["step"], row["status"]) for row in evidence["acceptance_observations"]],
                         [(11, "PASS"), (16, "PASS")])
        self.assertEqual(evidence["employer_observation"], {"synthetic_later_segment": True})

    def test_list_timeout_admits_only_base_public_steps(self):
        evidence, baseline, public, runtime = self.run_browser("timeout")
        self.assertNotIn("job_list_observation", evidence)
        joined = join_public_browser(baseline, public, evidence, _digest(public), _digest(evidence), runtime)
        self.assertEqual([(row["step"], row["status"]) for row in joined["acceptance_observations"]],
                         [(11, "PASS"), (16, "PASS")])

    def test_probe_egress_is_fatal_even_when_list_selector_fails(self):
        with self.assertRaisesRegex(ValueError, "^PUBLIC_BROWSER_EXTERNAL_EGRESS_ATTEMPTED$"):
            self.run_browser("egress", employer=True)

    def test_malformed_list_identity_remains_fatal(self):
        with self.assertRaisesRegex(ValueError, "^PUBLIC_BROWSER_JOB_LIST_READBACK_INVALID$"):
            self.run_browser("integrity", employer=True)


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class PublicBrowserAcceptanceTests(unittest.TestCase):
    def test_collected_jobs_are_read_only_visible_for_the_exact_profile(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-public-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            with fixture_server() as mock_origin:
                discover(store, mock_origin, "fixture")
                selected_job = next(row for row in store.jobs("fixture")
                                    if row["requisition"] == "B")
                application_id = prepare(store, selected_job["id"], "fixture", root)

                env = {
                    "MJOBS_DATABASE_URL": store.url,
                    "MJOBS_OPERATION_MODE": "FIXTURE",
                    "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                    "MJOBS_MOCK_ORIGIN": mock_origin,
                    "MJOBS_OUTPUT_DIR": str(root / "outputs"),
                }
                app = create_app(store, mock_origin, root / "outputs",
                                 redis_url="redis://127.0.0.1:1/0",
                                 operation_mode="FIXTURE")
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(
                    app, log_level="error", access_log=False,
                ))
                server_thread = threading.Thread(
                    target=server.run, kwargs={"sockets": [sock]}, daemon=True,
                )
                try:
                    with patch.dict(os.environ, env):
                        baseline = {
                            "source": {"commit": "a" * 40},
                            "profile_id": "fixture",
                            "application_id": application_id,
                            "operation_id": "trusted-ui-operation",
                            "request_id": "trusted-ui-request",
                            "selected_application_job_id": selected_job["id"],
                            "selected_application_route_id": store.application(application_id)["route_id"],
                            "external_submission_enabled": False,
                        }
                        executed = set()
                        fake_source_requests = []
                        payload = copy.deepcopy(RAW_REMOTIVE_PAYLOAD)
                        sibling = dict(payload["jobs"][0], id=1003,
                            url="https://remotive.com/remote-jobs/software-dev/senior-python-engineer-1003",
                            candidate_required_location="Canada Only")
                        payload["jobs"].append(sibling)
                        payload["job-count"] = len(payload["jobs"])

                        with TestClient(app) as api_client:
                            def request(method, path, **kwargs):
                                if method == "GET" and path.startswith("/operations/"):
                                    operation_id = path.rsplit("/", 1)[1]
                                    if operation_id not in executed:
                                        executed.add(operation_id)
                                        result = execute_operation(operation_id)
                                        self.assertEqual(result["status"], "COMPLETED", result)
                                response = api_client.request(method, path, **kwargs)
                                self.assertLess(response.status_code, 400, (path, response.text))
                                return response.json()

                            def controlled_provider_get(transport, request):
                                self.assertEqual(request.method, "GET")
                                self.assertEqual(str(request.url), "https://remotive.com/api/remote-jobs")
                                fake_source_requests.append(str(request.url))
                                return httpx.Response(200, json=payload, request=request)

                            with patch("httpx.HTTPTransport.handle_request", controlled_provider_get):
                                collected = collect_public_jobs(
                                    origin,
                                    baseline,
                                    root / "public.json",
                                    acceptance_run_id="controlled-public-browser-test",
                                    provider="remotive",
                                    request=request,
                                    execution_scope="CONTROLLED_TRANSPORT",
                                    containment_preflight=lambda *_: {
                                        "scheduler_contained": True,
                                        "source_policy_exclusive": True,
                                        "source_collection_exclusive": True,
                                        "owner": "isolated-public-browser-test",
                                        "token": "test-only-token",
                                        "lease_expires_at": "2099-01-01T00:00:00+00:00",
                                    },
                                )
                        self.assertEqual(collected["result"], "PASS", collected)
                        self.assertEqual(collected["execution_scope"], "CONTROLLED_TRANSPORT")
                        self.assertEqual(collected["acceptance_observations"][0]["status"], "NOT_RUN")
                        self.assertEqual(fake_source_requests, ["https://remotive.com/api/remote-jobs"])

                        server_thread.start()
                        deadline = time.monotonic() + 10
                        while not server.started:
                            if not server_thread.is_alive() or time.monotonic() > deadline:
                                self.fail("Isolated public-browser API did not start")
                            time.sleep(0.02)

                        runtime_identity = {
                            "source_manifest_sha256": "b" * 64,
                            "dockerfile_sha256": "c" * 64,
                            "images": {"api": "sha256:" + "d" * 64},
                            "frontend_assets": {"assets/index.js": "e" * 64},
                            "compose_files": ["private-path-is-projected-out"],
                        }
                        evidence = observe_public_browser(
                            origin, baseline, collected, runtime_identity=runtime_identity,
                        )

                finally:
                    server.should_exit = True
                    server_thread.join(timeout=10)
                    sock.close()
                    store.engine.dispose()

            self.assertFalse(server_thread.is_alive(), "Isolated public-browser API did not stop")
            self.assertEqual(evidence["result"], "PASS")
            self.assertEqual(evidence["evidence_kind"], "PUBLIC_BROWSER_READ_ONLY")
            self.assertEqual(evidence["profile_id"], "fixture")
            self.assertEqual(evidence["selected_application_job_id"], selected_job["id"])
            self.assertNotIn(selected_job["id"], evidence["observed_real_job_ids"])
            self.assertTrue(evidence["baseline_readback"]["unchanged"])
            self.assertEqual(evidence["runtime_identity"]["source_manifest_sha256"], "b" * 64)
            self.assertNotIn("compose_files", evidence["runtime_identity"])
            self.assertEqual(evidence["sha256"], hashlib.sha256(
                json.dumps(
                    {key: value for key, value in evidence.items() if key != "sha256"},
                    sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")
            ).hexdigest())

            first = next(row for row in evidence["observations"]
                         if row["native_posting_id"] == "1001")
            sibling_observation = next(row for row in evidence["observations"]
                                       if row["native_posting_id"] == "1003")
            self.assertNotEqual(first["job_id"], sibling_observation["job_id"])
            for posting in (first, sibling_observation):
                self.assertTrue(posting["checks"]["canonical_job_id_bound_to_selected_detail_readback"])
            self.assertEqual(first["restriction"]["source_field"], "candidate_required_location")
            self.assertEqual(first["restriction"]["source_value"], "USA Only")
            self.assertTrue(first["restriction"]["visible"])
            self.assertEqual(first["restriction"]["claim_scope"],
                             "SOURCE_REPORTED_LOCATION_ONLY; NOT_WORK_AUTHORIZATION")
            self.assertTrue(
                evidence["acceptance_observations"][1]["checks"]["source_remote_restriction_visible"]
            )
            self.assertEqual(evidence["browser"]["external_requests"], 0)
            self.assertEqual(evidence["browser"]["page_errors"], 0)
            self.assertTrue(evidence["browser"]["loopback_only"])
            self.assertTrue(evidence["browser"]["get_head_only"])

            self.assertEqual(evidence["acceptance_observations"][0]["step"], 11)
            self.assertEqual(evidence["acceptance_observations"][0]["status"], "NOT_RUN")
            for observation in evidence["observations"]:
                self.assertTrue(observation["checks"]["job_card_title_visible"])
                self.assertTrue(observation["checks"]["job_card_company_visible"])
                self.assertTrue(observation["checks"]["source_provider_visible"])
                self.assertTrue(observation["checks"]["application_provider_visible"])
                self.assertTrue(observation["checks"]["canonical_job_id_bound_to_selected_detail_readback"])
                self.assertTrue(observation["checks"]["native_posting_id_bound_to_source_snapshot"])
                self.assertTrue(observation["checks"]["source_reference_link_visible"])
                self.assertTrue(observation["checks"]["snapshot_hash_visible"])
            self.assertEqual(evidence["acceptance_observations"][1]["step"], 16)
            self.assertEqual(evidence["acceptance_observations"][1]["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()


class WwrPersistenceBrowserJoinTests(unittest.TestCase):
    def test_real_collection_api_and_browser_join_preserve_native_namespace(self):
        from cli.acceptance_public_browser import _join_postings
        from cli.acceptance_public_source import _public_jobs
        from services.discovery.sources.collect import configure_source, collect_source
        from tests.integration.test_wwr import RAW_WWR_RSS
        with tempfile.TemporaryDirectory(prefix="mjobs-wwr-join-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'join.db').as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(copy.deepcopy(PROFILE))
                source_id = configure_source(store, "default", provider="wwr")
                seen = []
                def wire(request):
                    self.assertEqual(str(request.url), "https://weworkremotely.com/remote-jobs.rss")
                    seen.append(str(request.url))
                    return httpx.Response(200, content=RAW_WWR_RSS,
                                          headers={"content-type": "application/rss+xml"})
                collect_source(store, source_id, "fixture", transport=httpx.MockTransport(wire))
                app = create_app(store, "http://127.0.0.1:1", root / "output",
                                 redis_url="redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                with TestClient(app) as client:
                    def request(method, path, **kwargs):
                        response = client.request(method, path, **kwargs)
                        self.assertEqual(response.status_code, 200)
                        return response.json()
                    source = next(r for r in request("GET", "/sources")["sources"] if r["id"] == source_id)
                    version = request("GET", "/profiles/fixture")["version"]
                    observations = _public_jobs(request, source,
                        {"started_at": "2000-01-01T00:00:00+00:00"},
                        {"job_id": "other-selected-fixture", "profile_version": version},
                        "fixture", "https://weworkremotely.com/remote-jobs.rss")
                    observation = observations[0]
                    job = request("GET", "/jobs/" + observation["job_id"], params={"profile_id": "fixture"})
                self.assertEqual(len(seen), 1)
                joined = _join_postings(job, observation, "wwr")
                self.assertTrue(joined)
                self.assertNotEqual(joined[0]["source_job_id"], observation["native_posting_id"])
                for change in ("native", "company", "source", "raw-storage-id", "random-storage-id", "posting-id", "origin", "snapshot"):
                    with self.subTest(change=change):
                        altered_job, altered_observation = copy.deepcopy(job), copy.deepcopy(observation)
                        posting = altered_job["postings"][0]
                        if change == "native": altered_observation["native_posting_id"] = "different-native"
                        elif change == "company": altered_job["company_id"] = "a" * 64
                        elif change == "source": altered_observation["source_id"] = "b" * 64
                        elif change == "raw-storage-id": posting["source_job_id"] = observation["native_posting_id"]
                        elif change == "random-storage-id": posting["source_job_id"] = "c" * 64
                        elif change == "posting-id": posting["id"] = "d" * 64
                        elif change == "origin": altered_observation["source_endpoint"] = "https://example.invalid/feed"
                        else: posting["raw"]["guid"] = "https://example.invalid/remote-jobs/" + observation["native_posting_id"]
                        with self.assertRaises(ValueError):
                            _join_postings(altered_job, altered_observation, "wwr")
            finally:
                store.engine.dispose()
