"""MJ-REQ-034 public ingestion oracle; only provider HTTP is synthetic."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class EducationNegationPublicIntakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-education-intake-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.url = "sqlite:///" + (self.root / "education.db").as_posix()
        self.store = Store(self.url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.enterContext(patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }))
        self.client = self.enterContext(TestClient(self._app()))
        self.assertEqual(self.client.put("/profiles", json=PROFILE).status_code, 200)
        response = self.client.put("/sources", json={
            "provider": "greenhouse", "tenant": "education-fixture",
            "poll_interval": 300, "request_timeout": 8,
            "rate_limit": 60, "jitter_seconds": 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.source_id = response.json()["id"]
        self.requests = []

    def _app(self):
        return create_app(self.store, "", self.root, operation_mode="PERSONAL")

    def _collect_and_reopen(self):
        rows = [{
            "id": identifier,
            "title": "Synthetic Education Control " + str(identifier),
            "location": {"name": "Remote - United States"},
            "content": "<p>" + clause + "</p>",
            "absolute_url": (
                "https://job-boards.greenhouse.io/education-fixture/jobs/"
                + str(identifier)
            ),
        } for identifier, clause in (
            (4101, "Bachelor's degree required."),
            (4102, "Bachelor's degree not required."),
        )]
        response = self.client.post("/sources/" + self.source_id + "/collect", json={
            "profile_id": "fixture", "request_id": "education-negation-public",
        })
        self.assertEqual(response.status_code, 202, response.text)
        operation = response.json()["operation_id"]

        def boundary(_transport, request):
            self.assertEqual((request.method, request.url.host),
                             ("GET", "boards-api.greenhouse.io"))
            self.requests.append(request.url.path)
            if request.url.path == "/v1/boards/education-fixture/jobs":
                return httpx.Response(200, json={"jobs": rows, "meta": {"total": 2}})
            self.assertEqual(request.url.path, "/v1/boards/education-fixture")
            return httpx.Response(200, json={"name": "Synthetic Education Employer"})

        with patch("httpx.HTTPTransport.handle_request", boundary):
            result = execute_operation(operation)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertIn("/v1/boards/education-fixture/jobs", self.requests)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self.enterContext(TestClient(self._app()))
        return self._jobs()

    def _jobs(self, **filters):
        response = self.client.get("/jobs", params={"profile_id": "fixture", **filters})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["jobs"]

    def test_positive_required_control_survives_public_readback(self):
        jobs = self._collect_and_reopen()
        self.assertEqual(len(jobs), 2)
        positive = next(j for j in jobs if j["attributes"]["native_posting_id"] == 4101)
        self.assertEqual(positive["attributes"]["education_requirement"], "BACHELORS_REQUIRED")
        self.assertIn(4101, [j["attributes"]["native_posting_id"] for j in
                            self._jobs(education_requirement="BACHELORS_REQUIRED")])

    def test_negated_degree_is_not_returned_by_required_education_filter(self):
        jobs = self._collect_and_reopen()
        self.assertEqual(len(jobs), 2)
        negated = next(j for j in jobs if j["attributes"]["native_posting_id"] == 4102)
        detail_response = self.client.get("/jobs/" + negated["id"], params={"profile_id": "fixture"})
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        detail = detail_response.json()
        self.assertIn("not required", detail["description"])
        self.assertTrue(any("not required" in p["raw"]["content"] for p in detail["postings"]))
        filtered = self._jobs(education_requirement="BACHELORS_REQUIRED")
        identifiers = sorted(j["attributes"]["native_posting_id"] for j in filtered)
        observation = {
            "source_clause": "Bachelor's degree not required.",
            "persisted_classification": negated["attributes"]["education_requirement"],
            "public_detail_classification": detail["attributes"]["education_requirement"],
            "required_filter_native_ids": identifiers,
            "expected_required_filter_native_ids": [4101],
            "database_reopened": True,
        }
        print(json.dumps(observation, sort_keys=True))
        self.assertEqual(identifiers, [4101], observation)
        self.assertNotEqual(negated["attributes"]["education_requirement"], "BACHELORS_REQUIRED")
