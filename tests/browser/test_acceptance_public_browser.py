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

import httpx
import uvicorn
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cli.acceptance_public_browser import run as observe_public_browser
from cli.acceptance_public_source import run as collect_public_jobs
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]


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
                                return httpx.Response(200, json=RAW_REMOTIVE_PAYLOAD, request=request)

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
