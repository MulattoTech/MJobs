"""Profile/job-scoped advisory risk-signal API acceptance tests."""
from __future__ import annotations

import http.client
import importlib
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class JobRiskSignalsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-job-risk-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'risk.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self._seed_job("fixture-risk-job", "fixture", description="Synthetic job text")
        self._seed_job("fixture-comparison", "fixture", company="Unrelated Employer",
                       description="Synthetic job text")
        self._seed_job("other-profile-comparison", "other", company="Other Employer",
                       description="Synthetic job text")
        self.evaluator_calls = []
        self.result_by_job = {}
        self.evaluator_exception = False
        try:
            router_module = importlib.import_module("apps.api.job_risk_signals")
            self.router_factory = router_module.create_job_risk_signals_router
        except ModuleNotFoundError as error:
            if error.name != "apps.api.job_risk_signals":
                raise
            self.router_factory = None
        app = FastAPI()
        if self.router_factory is not None:
            app.include_router(self.router_factory(self.store, evaluator=self._evaluate))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_job(self, job_id, profile_id, *, company="Synthetic Employer", description):
        company_id = f"company-{job_id}"
        profile_version = self.store.profile(profile_id)["version"]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": company, "domain": "",
            })
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": company_id, "requisition": job_id,
                "title": "Systems Administrator", "description": description,
                "skills": [], "location": "Remote", "remote_type": "UNKNOWN",
                "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": f"route-{job_id}", "job_id": job_id,
                "company_id": company_id, "provider": "manual", "tenant": "fixture",
                "requisition": job_id, "url": f"https://jobs.example.invalid/{job_id}",
            })
            upsert(connection, m.matches, {
                "id": f"match-{job_id}-{profile_id}", "job_id": job_id,
                "profile_id": profile_id, "profile_version": profile_version,
                "score": 50.0, "missing": [], "components": {},
            })

    def _evaluate(self, job, *, comparison_jobs=()):
        self.evaluator_calls.append({
            "job": deepcopy(job), "comparison_jobs": deepcopy(list(comparison_jobs)),
        })
        if self.evaluator_exception:
            raise RuntimeError("synthetic evaluator unavailable")
        return deepcopy(self.result_by_job.get(job["id"], {
            "warnings": [{
                "code": "PAYMENT_REQUEST", "level": "ADVISORY",
                "message": "Posting text requests an upfront payment.",
                "evidence": {
                    "field": "description", "source": "posting.description",
                    "text": "pay a processing fee",
                },
            }],
            "signals": [
                {"code": "PAYMENT_REQUEST", "state": "WARN"},
                {"code": "UNREALISTIC_SALARY", "state": "UNKNOWN",
                 "reason": "SALARY_CURRENCY_OR_PERIOD_UNKNOWN"},
                {"code": "EMPLOYER_IDENTITY_UNKNOWN", "state": "UNKNOWN",
                 "reason": "IDENTITY_NOT_VERIFIED"},
            ],
        }))

    def _url(self, profile_id="fixture", job_id="fixture-risk-job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/risk-signals"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: connection.execute(
                    select(func.count()).select_from(table),
                ).scalar_one()
                for table in m.metadata.sorted_tables
            }

    def _get_without_network(self, url):
        return self._get_client_without_network(self.client, url)

    def _get_client_without_network(self, client, url):
        with patch.object(socket, "create_connection",
                          side_effect=AssertionError("network access attempted")), \
                patch("urllib.request.urlopen",
                      side_effect=AssertionError("network access attempted")), \
                patch.object(http.client.HTTPConnection, "connect",
                             side_effect=AssertionError("network access attempted")):
            return client.get(url)

    def test_warning_preview_is_advisory_scoped_and_read_only(self):
        before = self._snapshot()

        response = self._get_without_network(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["job_id"], "fixture-risk-job")
        self.assertEqual(result["signals"], [{
            "code": "PAYMENT_REQUEST", "label": "Potential Risk",
            "severity": "ADVISORY",
            "reason": "Posting text requests an upfront payment.",
            "evidence_source": "posting.description",
            "observed_value": "pay a processing fee",
        }])
        self.assertEqual(result["unknowns"], [
            {"code": "UNREALISTIC_SALARY", "reason": "SALARY_CURRENCY_OR_PERIOD_UNKNOWN"},
            {"code": "EMPLOYER_IDENTITY_UNKNOWN", "reason": "IDENTITY_NOT_VERIFIED"},
        ])
        self.assertEqual(len(self.evaluator_calls), 1)
        self.assertEqual(self.evaluator_calls[0]["job"]["id"], "fixture-risk-job")
        comparison_ids = {row["id"] for row in self.evaluator_calls[0]["comparison_jobs"]}
        self.assertIn("fixture-comparison", comparison_ids)
        self.assertNotIn("other-profile-comparison", comparison_ids)
        self.assertEqual(self._snapshot(), before)
        self.assertNotIn("reject", result)
        self.assertEqual(self.store.job("fixture-risk-job", "fixture")["title"],
                         "Systems Administrator")

    def test_missing_scope_fails_closed_before_evaluator_and_future_evidence_is_unknown(self):
        before = self._snapshot()

        cross_profile = self._get_without_network(self._url("fixture", "other-profile-comparison"))
        self.assertEqual(cross_profile.status_code, 404, cross_profile.text)
        missing_job = self._get_without_network(self._url("fixture", "future-job"))
        self.assertEqual(missing_job.status_code, 404, missing_job.text)
        missing_profile = self._get_without_network(self._url("future-profile", "fixture-risk-job"))
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)
        self.assertEqual(self.evaluator_calls, [])
        self.assertEqual(self._snapshot(), before)

        self.result_by_job["fixture-risk-job"] = {"warnings": [], "signals": [
            {"code": "UNREALISTIC_SALARY", "state": "UNKNOWN", "reason": "SALARY_UNKNOWN"},
            {"code": "SUSPICIOUS_DOMAIN", "state": "UNKNOWN", "reason": "APPLICATION_URL_UNKNOWN"},
            {"code": "EMPLOYER_IDENTITY_UNKNOWN", "state": "UNKNOWN", "reason": "IDENTITY_UNKNOWN"},
        ]}
        future_data = self._get_without_network(self._url())
        self.assertEqual(future_data.status_code, 200, future_data.text)
        self.assertEqual(future_data.json()["signals"], [])
        self.assertEqual(future_data.json()["unknowns"], [
            {"code": "UNREALISTIC_SALARY", "reason": "SALARY_UNKNOWN"},
            {"code": "SUSPICIOUS_DOMAIN", "reason": "APPLICATION_URL_UNKNOWN"},
            {"code": "EMPLOYER_IDENTITY_UNKNOWN", "reason": "IDENTITY_UNKNOWN"},
        ])
        self.assertEqual(self._snapshot(), before)

    def test_invalid_or_unavailable_evaluator_fails_closed_without_writes(self):
        before = self._snapshot()
        self.result_by_job["fixture-risk-job"] = {"warnings": "not-a-list", "signals": []}

        malformed = self._get_without_network(self._url())

        self.assertEqual(malformed.status_code, 502, malformed.text)
        self.assertEqual(malformed.json()["detail"]["code"], "RISK_SIGNAL_RESULT_INVALID")
        self.assertEqual(self._snapshot(), before)

        self.result_by_job["fixture-risk-job"] = {
            "warnings": [],
            "signals": [{"code": "PAYMENT_REQUEST", "state": "WARN"}],
        }
        inconsistent = self._get_without_network(self._url())
        self.assertEqual(inconsistent.status_code, 502, inconsistent.text)
        self.assertEqual(inconsistent.json()["detail"]["code"], "RISK_SIGNAL_RESULT_INVALID")
        self.assertEqual(self._snapshot(), before)

        self.result_by_job.clear()
        self.evaluator_exception = True
        unavailable = self._get_without_network(self._url())

        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(unavailable.json()["detail"]["code"], "RISK_EVALUATION_UNAVAILABLE")
        self.assertEqual(self._snapshot(), before)

    def test_default_service_contract_maps_local_warning_and_unknowns(self):
        before = self._snapshot()
        app = FastAPI()
        app.include_router(self.router_factory(self.store))
        client = TestClient(app)
        self.addCleanup(client.close)

        response = self._get_client_without_network(client, self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["job_id"], "fixture-risk-job")
        self.assertIn("EMPLOYER_IDENTITY_UNKNOWN", {item["code"] for item in body["signals"]})
        self.assertTrue(all(item["label"] == "Potential Risk" for item in body["signals"]))
        self.assertTrue(all(item["severity"] == "ADVISORY" for item in body["signals"]))
        self.assertTrue(all(set(item) == {
            "code", "label", "severity", "reason", "evidence_source", "observed_value",
        } for item in body["signals"]))
        self.assertTrue(all(set(item) == {"code", "reason"} for item in body["unknowns"]))
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
