from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.recruiter_classification import create_recruiter_classification_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.recruiter_classification import classify_recruiter


PROFILE = {
    "id": "fixture", "name": "Synthetic User", "email": "user@example.test",
    "answers": [], "facts": [],
}


class RecruiterClassificationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-recruiter-classification-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'classification.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self._seed_job()
        self.classifier_calls = []
        app = FastAPI()
        app.include_router(create_recruiter_classification_router(
            self.store, classifier=self._classify,
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_job(self, *, attributes=None, job_id="job", company_id="company",
                  profile_id="fixture", profile_version=None):
        profile = self.store.profile(profile_id)
        with self.store.engine.begin() as connection:
            if connection.execute(select(m.companies.c.id).where(
                    m.companies.c.id == company_id)).first() is None:
                connection.execute(m.companies.insert().values(
                    id=company_id, name="Synthetic Employer", domain="employer.example",
                ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=f"req-{job_id}",
                title="Systems Engineer", description="Synthetic job description",
                skills=["linux"], location="Remote", remote_type="REMOTE",
                attributes=attributes or {},
            ))
            connection.execute(m.matches.insert().values(
                id=f"match-{profile_id}-{job_id}", job_id=job_id,
                profile_id=profile_id, profile_version=profile_version or profile["version"],
                score=72.5, missing=[], components={},
            ))

    @staticmethod
    def _evidence(classification="STAFFING_AGENCY", *, subject_type="JOB",
                  subject_id="job", evidence_ref="fixture://recruiter-classification/1",
                  current=True, explicit=True):
        return {
            "classification": classification,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "source": "synthetic_registry",
            "evidence_ref": evidence_ref,
            "current": current,
            "explicit": explicit,
            "confidence": 0.86,
        }

    def _classify(self, evidence, *, expected_job_id, expected_company_id):
        self.classifier_calls.append((deepcopy(evidence), expected_job_id, expected_company_id))
        return classify_recruiter(
            evidence,
            expected_job_id=expected_job_id,
            expected_company_id=expected_company_id,
        )

    def _url(self, profile_id="fixture", job_id="job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/recruiter-classification"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_staffing_classification_is_profile_job_scoped_and_read_only(self):
        claim = self._evidence()
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                attributes={"recruiter_classification_evidence": [claim]},
            ))
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture", "job_id": "job",
            "classification": "STAFFING_AGENCY", "confidence": 0.86,
            "reasons": ["Current explicit attributable evidence consistently identifies this as STAFFING_AGENCY."],
            "evidence": [claim], "unknowns": [], "advisory": True,
        })
        self.assertEqual(self.classifier_calls, [([claim], "job", "company")])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(before[m.matches.name][0]["profile_id"], "fixture")
        self.assertEqual(before[m.applications.name], [])
        self.assertEqual(before[m.operations.name], [])

    def test_default_router_invokes_the_conservative_classifier(self):
        claim = self._evidence("DIRECT_EMPLOYER")
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                attributes={"recruiter_classification_evidence": [claim]},
            ))
        app = FastAPI()
        app.include_router(create_recruiter_classification_router(self.store))
        client = TestClient(app)
        self.addCleanup(client.close)

        response = client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["classification"], "DIRECT_EMPLOYER")
        self.assertEqual(response.json()["evidence"], [claim])
        self.assertTrue(response.json()["advisory"])

    def test_stale_malformed_and_foreign_evidence_remain_unknown(self):
        cases = (
            ([self._evidence(current=False)], "STALE_OR_NONEXPLICIT_EVIDENCE_IGNORED"),
            (["untrusted text"], "INVALID_OR_UNATTRIBUTABLE_EVIDENCE"),
            ([self._evidence(subject_id="another-job")], "EVIDENCE_SUBJECT_MISMATCH"),
        )
        for evidence, unknown_code in cases:
            with self.subTest(unknown_code=unknown_code):
                with self.store.engine.begin() as connection:
                    connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                        attributes={"recruiter_classification_evidence": evidence},
                    ))
                before = self._snapshot()

                response = self.client.get(self._url())

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["profile_id"], "fixture")
                self.assertEqual(body["job_id"], "job")
                self.assertEqual(body["classification"], "UNKNOWN")
                self.assertIsNone(body["confidence"])
                self.assertEqual(body["evidence"], [])
                self.assertIn(unknown_code, body["unknowns"])
                self.assertTrue(body["advisory"])
                self.assertEqual(self._snapshot(), before)

    def test_absent_evidence_returns_explicit_unknown_without_writes(self):
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["classification"], "UNKNOWN")
        self.assertIsNone(response.json()["confidence"])
        self.assertEqual(response.json()["unknowns"], ["NO_CURRENT_ATTRIBUTABLE_EVIDENCE"])
        self.assertEqual(self.classifier_calls, [([], "job", "company")])
        self.assertEqual(self._snapshot(), before)

    def test_explicit_unknown_claim_keeps_its_evidence_provenance(self):
        claim = self._evidence("UNKNOWN")
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                attributes={"recruiter_classification_evidence": [claim]},
            ))
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["classification"], "UNKNOWN")
        self.assertIsNone(body["confidence"])
        self.assertEqual(body["evidence"], [claim])
        self.assertEqual(body["unknowns"], ["EXPLICIT_EVIDENCE_INCONCLUSIVE"])
        self.assertTrue(body["advisory"])
        self.assertEqual(self._snapshot(), before)

    def test_current_evidence_remains_visible_when_stale_evidence_is_ignored(self):
        current = self._evidence()
        stale = self._evidence("RECRUITER", current=False,
                               evidence_ref="fixture://recruiter-classification/old")
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                attributes={"recruiter_classification_evidence": [current, stale]},
            ))
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["classification"], "STAFFING_AGENCY")
        self.assertEqual(body["confidence"], 0.86)
        self.assertEqual(body["evidence"], [current])
        self.assertEqual(body["unknowns"], ["STALE_OR_NONEXPLICIT_EVIDENCE_IGNORED"])
        self.assertEqual(self._snapshot(), before)

    def test_missing_and_cross_profile_objects_fail_closed_without_writes(self):
        self._seed_job(job_id="other-job", profile_id="other")
        before = self._snapshot()

        missing_profile = self.client.get(self._url(profile_id="missing"))
        cross_profile = self.client.get(self._url(profile_id="other"))
        missing_job = self.client.get(self._url(job_id="missing-job"))

        self.assertEqual(missing_profile.status_code, 404)
        self.assertEqual(missing_profile.json()["detail"]["code"],
                         "RECRUITER_CLASSIFICATION_PROFILE_NOT_FOUND")
        self.assertEqual(cross_profile.status_code, 404)
        self.assertEqual(cross_profile.json()["detail"]["code"],
                         "RECRUITER_CLASSIFICATION_JOB_NOT_FOUND")
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(missing_job.json()["detail"]["code"],
                         "RECRUITER_CLASSIFICATION_JOB_NOT_FOUND")
        self.assertEqual(self.classifier_calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_stale_profile_job_match_is_rejected_before_classification(self):
        current = self.store.profile("fixture")
        changed = {
            **deepcopy(PROFILE),
            "facts": [{
                "id": "new-fact", "text": "Python", "kind": "skill",
                "source": "synthetic-test", "verified": True,
                "user_approved": True, "confidence": 100,
            }],
        }
        self.store.save_profile(changed, expected_version=current["version"])
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"],
                         "RECRUITER_CLASSIFICATION_STALE_CONTEXT")
        self.assertEqual(self.classifier_calls, [])
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
