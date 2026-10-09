"""The composed API mounts priority as a scoped, read-only advisory route."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store


class ApplicationPriorityAppMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-priority-mount-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'priority.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile({
            "id": "fixture", "name": "Synthetic User", "email": "user@example.test",
            "answers": [], "facts": [],
        })
        self.store.save_profile({
            "id": "other", "name": "Other Synthetic User", "email": "other@example.test",
            "answers": [], "facts": [],
        })
        self.now = datetime.now(timezone.utc)
        self._seed_job()
        app = create_app(
            self.store, "http://127.0.0.1:1", self.root,
            redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_job(self):
        company_id = "company-job"
        attributes = {
            "reported_posted_at": (self.now - timedelta(hours=12)).isoformat(),
            "expires_at": (self.now + timedelta(days=3)).isoformat(),
            "application_complexity": "LOW",
            "recruiter_classification_evidence": [{
                "classification": "STAFFING_AGENCY", "subject_type": "COMPANY",
                "subject_id": company_id, "source": "synthetic_source_context",
                "evidence_ref": "fixture://company-classification/1",
                "current": True, "explicit": True, "confidence": 0.9,
            }],
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Synthetic Employer", domain="employer.example",
            ))
            connection.execute(m.jobs.insert().values(
                id="job", company_id=company_id, requisition="req-job",
                title="Systems Engineer", description="Synthetic posting",
                skills=["linux"], location="Remote", remote_type="REMOTE",
                attributes=attributes,
            ))
            profile = self.store.profile("fixture")
            connection.execute(m.matches.insert().values(
                id="match-fixture-job", job_id="job", profile_id="fixture",
                profile_version=profile["version"], score=80.0, missing=[],
                components={"fit_factors": [{
                    "id": "salary", "label": "Salary expectation", "status": "ALIGNS",
                    "explanation": "Synthetic directly comparable salary ranges overlap.",
                    "score_effect": "NONE",
                }]},
            ))
            connection.execute(m.postings.insert().values(
                id="posting-job", job_id="job", source="synthetic_source",
                source_job_id="posting-job", source_url="https://source.example.test/job",
                application_url="https://apply.example.test/job", raw={},
                fetched_at=(self.now - timedelta(hours=12)).isoformat(),
            ))

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _url(self, profile_id="fixture", job_id="job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/application-priority"

    def test_composed_app_mounts_profile_scoped_read_only_advisory_priority(self):
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["job_id"], "job")
        self.assertEqual(body["match_score"], 80.0)
        self.assertNotEqual(body["priority_score"], body["match_score"])
        self.assertTrue(body["advisory"])
        self.assertFalse(body["affects_match_score"])
        factors = {row["key"]: row for row in body["factors"]}
        self.assertEqual(factors["direct_employer_availability"]["score"], 75.0)
        self.assertEqual(factors["salary"]["status"], "KNOWN")
        self.assertTrue(all(row["explanation"] for row in body["factors"]))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(before[m.matches.name][0]["score"], 80.0)
        self.assertEqual(before[m.applications.name], [])
        self.assertEqual(before[m.operations.name], [])

    def test_composed_route_hides_cross_profile_and_unknown_scope_without_writes(self):
        before = self._snapshot()

        foreign = self.client.get(self._url(profile_id="other"))
        missing = self.client.get(self._url(job_id="missing-job"))
        unknown_profile = self.client.get(self._url(profile_id="missing-profile"))

        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(foreign.json()["detail"]["code"], "APPLICATION_PRIORITY_JOB_NOT_FOUND")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "APPLICATION_PRIORITY_JOB_NOT_FOUND")
        self.assertEqual(unknown_profile.status_code, 404, unknown_profile.text)
        self.assertEqual(unknown_profile.json()["detail"]["code"], "APPLICATION_PRIORITY_PROFILE_NOT_FOUND")
        self.assertEqual(self._snapshot(), before)
