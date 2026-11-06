import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from cli.acceptance_source_setup import _policy_sha, compose_owned_public_containment


STAMP = "2026-11-06T18:00:00+00:00"
SOURCE = {
    "commit": "a" * 40,
    "tree": "b" * 40,
    "working_tree_diff_sha256": "c" * 64,
    "working_tree_status_sha256": "d" * 64,
}
BASELINE = {
    "source": SOURCE,
    "profile_id": "profile-1",
    "application_id": "application-1",
    "selected_application_job_id": "fixture-job-1",
    "external_submission_enabled": False,
}
RUNTIME = {"source_manifest_sha256": "e" * 64, "images": {"api": "sha256:" + "f" * 64}}
PROOF = {
    "scheduler_contained": True,
    "source_policy_exclusive": True,
    "source_collection_exclusive": True,
    "owner": "browser-test-owner",
    "token": "private-token",
    "lease_expires_at": "2026-11-06T18:05:00+00:00",
}


class SourceSetupBrowserTests(unittest.TestCase):
    def test_preexisting_extra_is_preserved_and_setup_fails_closed(self):
        existing = {"id": "operator-source", "provider": "remotive", "tenant": "default",
            "name": "Remotive", "enabled": False}
        writes = []

        def request(method, path, **kwargs):
            if method == "GET" and path == "/sources":
                return {"sources": [dict(existing)]}
            writes.append((method, path, kwargs))
            raise AssertionError((method, path))

        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "public-source-setup.json"
            wrapped = compose_owned_public_containment(
                lambda *_: dict(PROOF),
                base_url="http://127.0.0.1:18700", baseline=BASELINE,
                acceptance_run_id="step9-preexisting-extra", runtime_identity=RUNTIME,
                artifact_path=artifact_path, request=request, execution_scope="CONTROLLED_TRANSPORT",
                utc_now=lambda: datetime.fromisoformat(STAMP),
            )
            with self.assertRaisesRegex(ValueError, "EXTRA_SOURCE_MUST_BE_ABSENT"):
                wrapped("preflight", {"provider": "wwr", "base_url": "http://127.0.0.1:18700"})
            self.assertEqual(writes, [])
            self.assertFalse(artifact_path.exists())
            self.assertEqual(existing["enabled"], False)

    def test_extra_public_source_is_observed_then_disabled_before_release(self):
        rows = []
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path == "/sources":
                return {"sources": [dict(row) for row in rows]}
            if method == "GET" and path == "/operations":
                return {"operations": []}
            if method == "PUT" and path == "/sources":
                policy = dict(kwargs["json"])
                row = next((item for item in rows if (item["provider"], item["tenant"])
                            == (policy["provider"], policy["tenant"])), None)
                if row is None:
                    row = {"id": "source-" + policy["provider"], "name": policy["provider"].title()}
                    rows.append(row)
                row.update(policy)
                row.update(strategy="FEED", capabilities={"direct_application": False},
                           last_attempt=None, last_success=None, jobs_seen=0)
                return {"id": row["id"]}
            raise AssertionError((method, path))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.route("**/*", lambda route: _route_ui_request(route, rows))
                page.goto("http://127.0.0.1:18700/", wait_until="domcontentloaded")
                with tempfile.TemporaryDirectory() as directory:
                    artifact_path = Path(directory) / "public-source-setup.json"
                    stages = []

                    def containment(stage, context):
                        stages.append(stage)
                        return dict(PROOF)

                    wrapped = compose_owned_public_containment(
                        containment,
                        base_url="http://127.0.0.1:18700",
                        baseline=BASELINE,
                        acceptance_run_id="step9-browser-contract",
                        runtime_identity=RUNTIME,
                        artifact_path=artifact_path,
                        request=request,
                        page=page,
                        execution_scope="CONTROLLED_TRANSPORT",
                        utc_now=lambda: datetime.fromisoformat(STAMP),
                    )
                    wrapped("preflight", {"provider": "wwr", "base_url": "http://127.0.0.1:18700"})
                    self.assertEqual([row["provider"] for row in rows], ["remotive"])

                    primary_policy = {
                        "provider": "wwr", "tenant": "default", "enabled": True,
                        "poll_interval": 604800, "concurrency": 1, "request_timeout": 10,
                        "rate_limit": 1, "jitter_seconds": 0,
                    }
                    request("PUT", "/sources", json=primary_policy)
                    primary = next(row for row in rows if row["provider"] == "wwr")
                    wrapped("dispatch", {"provider": "wwr", "base_url": "http://127.0.0.1:18700",
                        "source_id": primary["id"], "source_policy_sha256": _policy_sha(primary)})
                    extra = next(row for row in rows if row["provider"] == "remotive")
                    self.assertFalse(extra["enabled"])
                    self.assertIsNone(extra["last_attempt"])
                    self.assertIsNone(extra["last_success"])
                    self.assertEqual(extra["jobs_seen"], 0)

                    wrapped("release", {"provider": "wwr", "base_url": "http://127.0.0.1:18700",
                        "cleanup_status": "RESTORED_DISABLED"})
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    self.assertEqual(artifact["acceptance_observations"][0]["step"], 9)
                    self.assertEqual(artifact["acceptance_observations"][0]["evidence_kind"], "PUBLIC_READ_ONLY")
                    self.assertEqual(artifact["acceptance_observations"][0]["status"], "NOT_RUN")
                    self.assertEqual(artifact["owned_extra"]["source_id"], extra["id"])
                    self.assertEqual(artifact["owned_extra"]["disabled"], True)
                    self.assertEqual(artifact["extra_collection_operation_ids"], [])
                    self.assertNotIn("/collect", [path for _, path, _ in calls])
                    self.assertLess(stages.index("finalize"), stages.index("release"))
            finally:
                browser.close()


def rows_update_from_ui(body, rows):
    import json as json_module

    payload = json_module.loads(body)
    row = next((item for item in rows if (item["provider"], item["tenant"])
                == (payload["provider"], payload["tenant"])), None)
    if row is None:
        row = {"id": "source-" + payload["provider"], "name": payload["provider"].title()}
        rows.append(row)
    row.update(payload)
    row.update(strategy="FEED", capabilities={"direct_application": False},
               last_attempt=None, last_success=None, jobs_seen=0)
    return json.dumps({"id": row["id"]})


def _route_ui_request(route, rows):
    request = route.request
    if request.url == "http://127.0.0.1:18700/":
        route.fulfill(status=200, content_type="text/html", body=_sources_ui())
    elif request.url == "http://127.0.0.1:18700/sources" and request.method == "GET":
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"sources": rows}))
    elif request.url == "http://127.0.0.1:18700/sources" and request.method == "PUT":
        route.fulfill(status=200, content_type="application/json",
                      body=rows_update_from_ui(request.post_data, rows))
    else:
        route.abort()


def _sources_ui():
    return """
    <nav aria-label="Main navigation"><button type="button">Sources</button></nav>
    <form id="source-form">
      <label>Source provider<select aria-label="Source provider">
        <option value="wwr">We Work Remotely</option><option value="remotive">Remotive</option>
      </select></label>
      <label>Interval (minutes)<input aria-label="Interval (minutes)" value="60"></label>
      <label>Request timeout (seconds)<input aria-label="Request timeout (seconds)" value="10"></label>
      <label>Rate limit (requests/minute)<input aria-label="Rate limit (requests/minute)" value="60"></label>
      <label>Source concurrency<input aria-label="Source concurrency" value="1"></label>
      <label>Schedule jitter (seconds)<input aria-label="Schedule jitter (seconds)" value="30"></label>
      <button type="submit">Add source</button>
    </form><main id="sources"></main>
    <script>
      async function load(){const rows=await (await fetch('/sources')).json();
        document.querySelector('#sources').innerHTML=rows.sources.map(s=>`<section><h2>${s.name}</h2>
          <button type="button">${s.enabled?'Disable':'Enable'}</button></section>`).join('');
        document.querySelectorAll('#sources button').forEach((button,i)=>button.onclick=async()=>{
          const rows=await (await fetch('/sources')).json(); const s=rows.sources[i];
          await fetch('/sources',{method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({provider:s.provider,tenant:s.tenant,enabled:!s.enabled,
              poll_interval:s.poll_interval,concurrency:s.concurrency,request_timeout:s.request_timeout,
              rate_limit:s.rate_limit,jitter_seconds:s.jitter_seconds})}); await load();});
      }
      document.querySelector('#source-form').onsubmit=async e=>{e.preventDefault();
        await fetch('/sources',{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({provider:document.querySelector('[aria-label="Source provider"]').value,
            tenant:'default',enabled:true,poll_interval:Number(document.querySelector('[aria-label="Interval (minutes)"]').value)*60,
            request_timeout:Number(document.querySelector('[aria-label="Request timeout (seconds)"]').value),
            rate_limit:Number(document.querySelector('[aria-label="Rate limit (requests/minute)"]').value),
            concurrency:Number(document.querySelector('[aria-label="Source concurrency"]').value),
            jitter_seconds:Number(document.querySelector('[aria-label="Schedule jitter (seconds)"]').value)})});
        await load();}; load();
    </script>"""


if __name__ == "__main__":
    unittest.main()
