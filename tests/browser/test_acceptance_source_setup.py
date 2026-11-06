import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from cli.acceptance_source_setup import _policy_sha, compose_owned_public_containment
from packages.persistence.store import identity
from services.discovery.sources.registry import source_adapter


_TEST_NOW = datetime.now(timezone.utc)
STAMP = (_TEST_NOW - timedelta(seconds=2)).isoformat()
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
    "lease_expires_at": (_TEST_NOW + timedelta(minutes=5)).isoformat(),
}


class SourceSetupBrowserTests(unittest.TestCase):
    def test_preexisting_extra_is_preserved_and_setup_fails_closed(self):
        existing = {"id": "operator-source", "provider": "remotive", "tenant": "default",
            "name": "default", "enabled": False}
        writes = []

        def request(method, path, **kwargs):
            if method == "GET" and path in {"/settings", "/health"}:
                return {"operation_mode": "FIXTURE", "external_submission_enabled": False}
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
            if method == "GET" and path in {"/settings", "/health"}:
                return {"operation_mode": "FIXTURE", "external_submission_enabled": False}
            if method == "GET" and path == "/sources":
                return {"sources": [dict(row) for row in rows]}
            if method == "GET" and path == "/operations":
                return {"operations": []}
            if method == "PUT" and path == "/sources":
                policy = dict(kwargs["json"])
                row = next((item for item in rows if (item["provider"], item["tenant"])
                            == (policy["provider"], policy["tenant"])), None)
                configured = _configured_source(policy, previous=row)
                if row is None:
                    rows.append(configured)
                    source_id = configured["id"]
                else:
                    row.clear()
                    row.update(configured)
                    source_id = row["id"]
                return {"id": source_id}
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
                    source_events = []
                    class Containment:
                        def __call__(self, stage, context):
                            stages.append(stage)
                            if source_events and "additional_source" in context:
                                self.assert_binding(context["additional_source"])
                            if stage == "release":
                                return {"released": True, "scheduler_contained": False,
                                    "source_policy_exclusive": False, "source_collection_exclusive": False,
                                    "additional_source_exclusive": False, "project": "fixture-project",
                                    "scheduler_container_id": "fixture-scheduler",
                                    "lease_expired_at": STAMP}
                            return dict(PROOF)

                        def assert_binding(self, binding):
                            if binding.get("provider") != "remotive" or binding.get("tenant") != "default":
                                raise AssertionError(binding)
                            if binding.get("source_id") is None:
                                if (binding.get("enabled_policy_sha256") is not None
                                        or binding.get("disabled_policy_sha256") is not None
                                        or binding.get("enabled") is not False):
                                    raise AssertionError(binding)
                            elif (not isinstance(binding.get("source_id"), str)
                                  or not isinstance(binding.get("enabled_policy_sha256"), str)
                                  or binding.get("enabled") not in {True, False}
                                  or binding.get("enabled") is False
                                  and not isinstance(binding.get("disabled_policy_sha256"), str)):
                                raise AssertionError(binding)

                        def bind_additional_source(self, *, policy, primary_context):
                            if policy["provider"] != "remotive" or policy["tenant"] != "default":
                                raise AssertionError(policy)
                            source_events.append(("bind", dict(policy)))
                            return {"additional_source_absent": True, "provider": "remotive",
                                "tenant": "default", "lease_expires_at": PROOF["lease_expires_at"],
                                "lease_model": "LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS"}

                        def additional_source(self, event, context):
                            source_events.append((event, dict(context)))
                            return {"additional_source_policy_verified": True,
                                "additional_source_id": context["additional_source_id"],
                                "additional_source_enabled": context["additional_enabled"],
                                "lease_expires_at": PROOF["lease_expires_at"]}

                        @staticmethod
                        def policy_sha256(source):
                            return _policy_sha(source)

                    wrapped = compose_owned_public_containment(
                        Containment(),
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

                    release_context = {"provider": "wwr", "base_url": "http://127.0.0.1:18700",
                        "cleanup_status": "RESTORED_DISABLED"}
                    wrapped("release", release_context)
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    self.assertEqual(artifact["schema_version"], 2)
                    self.assertEqual(artifact["acceptance_observations"][0]["step"], 9)
                    self.assertEqual(artifact["acceptance_observations"][0]["evidence_kind"], "PUBLIC_READ_ONLY")
                    self.assertEqual(artifact["acceptance_observations"][0]["status"], "NOT_RUN")
                    self.assertEqual(artifact["owned_extra"]["source_id"], extra["id"])
                    self.assertEqual(artifact["owned_extra"]["disabled"], True)
                    self.assertEqual(artifact["extra_collection_operation_ids"], [])
                    self.assertEqual(artifact["extra_operation_readback_sha256"],
                                     __import__("hashlib").sha256(b"[]").hexdigest())
                    policy_stages = [row["stage"] for row in artifact["extra_policy_readbacks"]]
                    self.assertEqual(policy_stages[:5], [
                        "preflight_absent", "immediately_before_create", "created_enabled",
                        "before_disable", "disabled_readback",
                    ])
                    self.assertEqual(policy_stages[5:], ["release"])
                    operation_stages = [row["stage"] for row in artifact["extra_operation_readbacks"]]
                    self.assertEqual(operation_stages[:2], [
                        "created_before_primary_collection", "disabled_before_primary_collection",
                    ])
                    self.assertEqual(operation_stages[2:], ["release"])
                    self.assertIn("release", [row["stage"] for row in artifact["primary_containment_checks"]])
                    self.assertEqual([row["receipt"].get("released") for row in artifact["primary_containment_checks"]
                                      if row["stage"] == "release"], [True])
                    self.assertTrue(artifact["release_receipt"]["released"])
                    self.assertEqual(artifact["release_receipt"]["receipt"]["project"], "fixture-project")
                    self.assertEqual(artifact["release_receipt"]["binding"]["extra_source_id"], extra["id"])
                    self.assertNotIn('"token":', json.dumps(artifact))
                    self.assertTrue(all(row["verified"] is False
                                        for row in artifact["primary_containment_checks"]))
                    self.assertNotIn("/collect", [path for _, path, _ in calls])
                    self.assertLess(stages.index("finalize"), stages.index("release"))
                    self.assertEqual(source_events[0][0], "bind")
                    self.assertIn("created", [event for event, _ in source_events])
                    self.assertIn("before_disable", [event for event, _ in source_events])
                    self.assertIn("disabled", [event for event, _ in source_events])
                    self.assertIn("release", [event for event, _ in source_events])
            finally:
                browser.close()


def rows_update_from_ui(body, rows):
    import json as json_module

    payload = json_module.loads(body)
    row = next((item for item in rows if (item["provider"], item["tenant"])
                == (payload["provider"], payload["tenant"])), None)
    configured = _configured_source(payload, previous=row)
    if row is None:
        rows.append(configured)
        source_id = configured["id"]
    else:
        row.clear()
        row.update(configured)
        source_id = row["id"]
    return json.dumps({"id": source_id})


def _configured_source(policy, *, previous=None):
    capabilities = source_adapter({
        "provider": policy["provider"], "tenant": policy["tenant"],
        "request_timeout": policy["request_timeout"],
        "rate_limit": policy["rate_limit"], "concurrency": policy["concurrency"],
    }).capabilities()
    return {
        "id": identity(policy["provider"], policy["tenant"]),
        "provider": policy["provider"], "tenant": policy["tenant"], "name": policy["tenant"],
        **policy,
        "strategy": capabilities.strategy.value,
        "capabilities": capabilities.as_dict(),
        "health_status": "NOT_CHECKED",
        "last_attempt": previous.get("last_attempt") if previous else None,
        "last_success": previous.get("last_success") if previous else None,
        "jobs_seen": previous.get("jobs_seen", 0) if previous else 0,
    }


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
    </form><p role="status" id="notice"></p><main id="sources"></main>
    <script>
      async function load(){const rows=await (await fetch('/sources')).json();
        document.querySelector('#sources').innerHTML=rows.sources.map(s=>`<section class="panel">
          <div class="toolbar"><div><h2>${s.name}</h2><p>${s.provider} / ${s.tenant}</p></div>
          <span class="tag">${s.enabled?'HEALTHY':'DISABLED'}</span></div>
          <p>FEED · every ${s.poll_interval/60} minutes</p><button type="button">Collect now</button>
          <button class="quiet" type="button">${s.enabled?'Disable':'Enable'}</button></section>`).join('');
        document.querySelectorAll('#sources section').forEach((section,i)=>section.querySelectorAll('button')[1].onclick=async()=>{
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
        await load(); document.querySelector('#notice').textContent='Source saved. Create a search profile below to enable scheduled discovery.';}; load();
    </script>"""


if __name__ == "__main__":
    unittest.main()
