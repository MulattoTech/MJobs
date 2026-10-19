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

    def _collect_and_reopen(self, clauses=None):
        clauses = clauses or (
            (4101, "Bachelor's degree required."),
            (4102, "Bachelor's degree not required."),
        )
        rows = [{
            "id": identifier,
            "title": "Synthetic Education Control " + str(identifier),
            "location": {"name": "Remote - United States"},
            "content": "<p>" + clause + "</p>",
            "absolute_url": (
                "https://job-boards.greenhouse.io/education-fixture/jobs/"
                + str(identifier)
            ),
        } for identifier, clause in clauses]
        response = self.client.post("/sources/" + self.source_id + "/collect", json={
            "profile_id": "fixture", "request_id": "education-negation-public",
        })
        self.assertEqual(response.status_code, 202, response.text)
        operation = response.json()["operation_id"]
        self.operation_id = operation

        def boundary(_transport, request):
            self.assertEqual((request.method, request.url.host),
                             ("GET", "boards-api.greenhouse.io"))
            self.requests.append(request.url.path)
            if request.url.path == "/v1/boards/education-fixture/jobs":
                return httpx.Response(200, json={"jobs": rows, "meta": {"total": len(rows)}})
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

    def test_categories_typography_conflicts_and_matching_survive_reopen_and_replay(self):
        examples = (
            (4201, "Bachelor’s degree required.", "BACHELORS_REQUIRED"),
            (4202, "Bachelor's degree preferred.", "BACHELORS_PREFERRED"),
            (4203, "Bachelor's degree or equivalent experience required.", "BACHELORS_OR_EQUIVALENT_EXPERIENCE"),
            (4204, "College degree preferred.", "DEGREE_PREFERRED"),
            (4205, "Python experience required.", "NONE_SPECIFIED"),
            (4206, "Bachelor's degree isn't required.", "NONE_SPECIFIED"),
            (4207, "No bachelor's degree required.", "NONE_SPECIFIED"),
            (4208, "Bachelor's degree not required, but preferred.", "BACHELORS_PREFERRED"),
            (4209, "Bachelor's degree not required; security clearance required.", "NONE_SPECIFIED"),
            (4210, "Bachelor's degree preferred. Bachelor's degree required.", "BACHELORS_REQUIRED"),
            (4211, "Bachelor's degree required, not preferred.", "BACHELORS_REQUIRED"),
            (4212, "Bachelor's degree required or equivalent experience not accepted.", "BACHELORS_REQUIRED"),
        )
        self._assert_categories_and_matching(examples)

    def test_field_list_qualifiers_survive_public_intake_filters_matching_and_reopen(self):
        examples = (
            (4301, "Bachelor's degree in Computer Science, Engineering, or a related field required.", "BACHELORS_REQUIRED"),
            (4302, "Bachelor’s degree in Computer Science, Engineering, or a related field preferred.", "BACHELORS_PREFERRED"),
            (4303, "Bachelor's degree in Computer Science, Engineering, or equivalent experience required.", "BACHELORS_OR_EQUIVALENT_EXPERIENCE"),
            (4304, "Degree in Computer Science, Engineering, or a related field preferred.", "DEGREE_PREFERRED"),
            (4305, "Bachelor's degree in Computer Science, Engineering, or a related field not required.", "NONE_SPECIFIED"),
            (4306, "Bachelor's degree in Computer Science, Engineering, or a related field preferred, Python required.", "BACHELORS_PREFERRED"),
            (4307, "Bachelor's degree in Computer Science, security clearance required.", "NONE_SPECIFIED"),
            (4308, "Bachelor's degree, Python required.", "NONE_SPECIFIED"),
        )
        self._assert_categories_and_matching(examples)

    def _assert_categories_and_matching(self, examples):
        jobs = self._collect_and_reopen([(identifier, clause) for identifier, clause, _ in examples])
        by_native = {job['attributes']['native_posting_id']: job for job in jobs}
        self.assertEqual(len(by_native), len(examples))
        for identifier, clause, category in examples:
            with self.subTest(clause=clause):
                job = by_native[identifier]
                response = self.client.get('/jobs/' + job['id'], params={'profile_id': 'fixture'})
                self.assertEqual(response.status_code, 200, response.text)
                detail = response.json()
                self.assertIn(clause, detail['description'])
                self.assertTrue(any(clause in posting['raw']['content'] for posting in detail['postings']))
                factors = [factor for factor in detail['match']['components']['fit_factors']
                           if factor['id'].startswith('education:')]
                self.assertEqual((job['attributes']['education_requirement'], len(factors)),
                                 (category, 0 if category == 'NONE_SPECIFIED' else 1))
                if category == 'NONE_SPECIFIED':
                    self.assertEqual(factors, [])
                else:
                    self.assertEqual(len(factors), 1)
                    self.assertEqual(factors[0]['status'], 'UNKNOWN')
                    self.assertEqual(factors[0]['score_effect'], 'NONE')
        for category in {row[2] for row in examples}:
            with self.subTest(filter=category):
                self.assertEqual(
                    sorted(job['attributes']['native_posting_id'] for job in self._jobs(education_requirement=category)),
                    sorted(identifier for identifier, _, expected in examples if expected == category))
        request_count = len(self.requests)
        with patch('httpx.HTTPTransport.handle_request', side_effect=AssertionError('Replay must not refetch')):
            replay = execute_operation(self.operation_id)
        self.assertEqual(replay['status'], 'ALREADY_CLAIMED_OR_CAPACITY', replay)
        operation = self.client.get('/operations/' + self.operation_id)
        self.assertEqual(operation.status_code, 200, operation.text)
        self.assertEqual(operation.json()['state'], 'COMPLETED')
        self.assertEqual(len(self.requests), request_count)
        self.assertEqual(len(self._jobs()), len(examples))

    def test_bracketed_and_comma_only_subjects_survive_intake_filter_and_matching(self):
        examples = (
            (4401, "Bachelor's degree (Computer Science, Engineering, or related field) required.", 'BACHELORS_REQUIRED'),
            (4402, "Bachelor's degree (Computer Science, Engineering, or related field) preferred.", 'BACHELORS_PREFERRED'),
            (4403, "Bachelor's degree in Computer Science, Engineering required.", 'BACHELORS_REQUIRED'),
            (4404, "Bachelor's degree in Computer Science, Engineering preferred.", 'BACHELORS_PREFERRED'),
            (4405, "Bachelor's degree (Computer Science, Engineering), or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            (4406, "Bachelor's degree Computer Science, Engineering or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            (4407, "Bachelor's degree (Computer Science, Engineering) not required.", 'NONE_SPECIFIED'),
            (4408, "Bachelor's degree (Computer Science, Engineering), Python required.", 'NONE_SPECIFIED'),
            (4409, "Degree (Computer Science, Engineering) preferred.", 'DEGREE_PREFERRED'),
            (4410, "Bachelor's degree in Computer Science, Engineering not required, security clearance mandatory.", 'NONE_SPECIFIED'),
            (4411, "Bachelor's degree, Computer Science, Engineering required.", 'BACHELORS_REQUIRED'),
            (4412, "Bachelor's degree (Computer Science, Engineering) optional, Python required.", 'NONE_SPECIFIED'),
        )
        self._assert_categories_and_matching(examples)

    def test_completed_field_subject_cannot_import_an_unrelated_qualification(self):
        examples = (
            (4501, "Bachelor's degree in Computer Science or Engineering, Python required.", 'NONE_SPECIFIED'),
            (4502, "Bachelor's degree in Computer Science or Engineering, Python preferred.", 'NONE_SPECIFIED'),
            (4503, "Bachelor's degree in Computer Science and Engineering, Zorblatt tooling required.", 'NONE_SPECIFIED'),
            (4504, "Bachelor’s degree (Computer Science or Engineering), incident response preferred.", 'NONE_SPECIFIED'),
            (4505, "Degree [Computer Science [Systems, Networks] or Engineering]: French preferred.", 'NONE_SPECIFIED'),
            (4506, "Bachelor's degree in Computer Science or Engineering — Kubernetes operations required.", 'NONE_SPECIFIED'),
            (4507, "Bachelor's degree in Computer Science or Engineering preferred, Python required.", 'BACHELORS_PREFERRED'),
            (4508, "Bachelor's degree in Computer Science or Engineering required, Zorblatt tooling preferred.", 'BACHELORS_REQUIRED'),
            (4509, "Bachelor's degree in Computer Science or Engineering, or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            (4510, "Degree in Computer Science or Engineering, French preferred.", 'NONE_SPECIFIED'),
        )
        self._assert_categories_and_matching(examples)

    def test_multiple_join_subjects_and_attachment_limits_survive_public_readback(self):
        examples = (
            (4601, "Bachelor's degree in Computer Science and Engineering, or a related field required.", 'BACHELORS_REQUIRED'),
            (4602, "Bachelor's degree in Computer Science and Engineering, or a related field preferred.", 'BACHELORS_PREFERRED'),
            (4603, "Bachelor's degree in Computer Science or Engineering or Mathematics required.", 'BACHELORS_REQUIRED'),
            (4604, "Bachelor's degree in Computer Science or Engineering or Mathematics preferred.", 'BACHELORS_PREFERRED'),
            (4605, "Bachelor's degree (Computer Science [Systems or Networking] and Engineering, or related field) or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            (4606, "Bachelor's degree in Computer Science and Engineering, or a related field, Python required.", 'NONE_SPECIFIED'),
            (4607, "Bachelor's degree in Computer Science or Engineering or Mathematics, Zorblatt tooling preferred.", 'NONE_SPECIFIED'),
            (4608, "Bachelor's degree (Computer Science or Engineering), Mathematics required.", 'NONE_SPECIFIED'),
            (4609, "Bachelor's degree in Xenobiology required.", 'BACHELORS_REQUIRED'),
            (4610, "Bachelor's degree in Comparative Literature preferred.", 'BACHELORS_PREFERRED'),
            (4611, "Degree in Food Science preferred.", 'DEGREE_PREFERRED'),
            (4612, "Bachelor's degree in Xenobiology, Marmalade Studies required.", 'NONE_SPECIFIED'),
            (4613, "Bachelor's degree in Computer Science or Engineering or Mathematics not required, but preferred.", 'BACHELORS_PREFERRED'),
            (4614, "Bachelor's degree in Computer Science or Engineering or Mathematics not required or mandatory.", 'NONE_SPECIFIED'),
            (4615, "Bachelor's degree in Computer Science and Engineering required or equivalent experience not accepted.", 'BACHELORS_REQUIRED'),
        )
        self._assert_categories_and_matching(examples)
