"""Keep validated native apply evidence bound through ATS enrollment.

All provider and employer HTTP boundaries are controlled. The production source
snapshot, profile authorization, public-client redirect/robots path, and domain
and source persistence remain active; no live provider or form submission runs.
"""
import unittest
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.domain_acquisition import native_job_link
from services.company_discovery.ats_enrollment import _validate_native_apply_handoff
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.public_client import PublicFetchError
from tests.integration.test_official_domain_canonical_apply_proof import _apply_page, _job_page
from workers.tasks import execute_operation


class NativeApplyEnrollmentHandoffTests(unittest.TestCase):
    def setUp(self):
        from tests.integration.test_official_domain_acquisition import OfficialDomainAcquisitionTests

        self.case = OfficialDomainAcquisitionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        self.request = self.case._collect()
        with self.case.store.engine.connect() as connection:
            request = connection.execute(select(m.company_discovery_requests)).mappings().one()
            self.proof = native_job_link(connection, request)
        self.assertIsNotNone(self.proof)
        self.job_url = self.proof["website"]
        parsed = urlsplit(self.job_url)
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self.apply_url = f"{parsed.scheme}://{parsed.netloc}/careers/apply/python/1234"
        self.job_html = _job_page(
            canonical=canonical,
            job_url=canonical,
            job_id=self.proof["native_job_id"],
            title=self.proof["title"],
            company="Example Employer",
            apply_href=self.apply_url,
        )
        self.apply_html = _apply_page(
            iframe_src=(
                "https://job-boards.greenhouse.io/embed/job_app?for="
                + self.proof["tenant"] + "&token=" + self.proof["native_job_id"]
            )
        )
        self.employer_requests = []

    def _employer_request_once(self, url, _deadline):
        self.employer_requests.append(url)
        request = httpx.Request("GET", url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if url == self.job_url:
            return httpx.Response(200, text=self.job_html, request=request)
        if url == self.apply_url:
            return httpx.Response(200, text=self.apply_html, request=request)
        return httpx.Response(404, request=request)

    def test_validated_apply_proof_reaches_native_ats_enrollment(self):
        with patch(
            "httpx.HTTPTransport.handle_request",
            lambda transport, request: self.case._native_boundary(transport, request),
        ), patch.object(
            PublicCompanyClient, "_request_once",
            lambda _client, url, deadline: self._employer_request_once(url, deadline),
        ), patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0):
            outcome = execute_operation(self.request["operation_id"])

        self.assertEqual(outcome["status"], "COMPLETED", outcome)
        self.assertEqual(self.employer_requests.count(self.apply_url), 1)
        self.assertNotIn(
            "https://job-boards.greenhouse.io/embed/job_app?for="
            + self.proof["tenant"] + "&token=" + self.proof["native_job_id"],
            self.employer_requests,
        )
        with self.case.store.engine.connect() as connection:
            sources = list(connection.execute(select(m.company_sources).where(
                m.company_sources.c.career_url.is_not(None),
            )).mappings())
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["provider"], "greenhouse")
            self.assertEqual(sources[0]["tenant"], self.proof["tenant"])
            self.assertEqual(sources[0]["career_url"], self.job_url)
            self.assertTrue(sources[0]["discovered_automatically"])
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == "COMPANY_SOURCE_DISCOVERED",
                m.audit.c.entity_id == self.request["company_id"],
            )).mappings().one()
        self.assertEqual(audit["payload"]["tenant"], self.proof["tenant"])

    def test_stale_or_mismatched_handoff_identity_is_rejected(self):
        authorization = {
            "authorization": "ATS_JOB_LINK_VERIFIED",
            "request_id": self.request["id"],
            "company_id": self.request["company_id"],
            "native_job_link": dict(self.proof),
        }
        proof = {
            **{key: authorization["native_job_link"].get(key) for key in (
                "source_id", "collector_source_id", "candidate_id", "snapshot_id",
                "snapshot_sha256", "quality_observation_id", "provider", "tenant",
                "native_job_id", "title", "profile_id", "profile_version")},
            "request_id": authorization["request_id"],
            "company_id": authorization["company_id"],
            "reciprocal_method": "SAME_ORIGIN_APPLY_PAGE",
            "employer_url": self.job_url,
            "employer_response_sha256": "a" * 64,
            "apply_url": self.apply_url,
            "apply_response_sha256": "b" * 64,
        }
        self.assertEqual(_validate_native_apply_handoff(
            proof, authorization, self.job_url, self.proof["profile_id"],
            self.proof["profile_version"]), self.proof["tenant"])
        for field, changed_value in (
                ("source_id", "stale-source"),
                ("profile_version", "stale-profile-version"),
                ("tenant", "different-board"),
                ("native_job_id", "9999"),
                ("apply_url", "https://other.example/careers/apply/1234")):
            with self.subTest(field=field):
                changed = dict(proof, **{field: changed_value})
                with self.assertRaises(PublicFetchError):
                    _validate_native_apply_handoff(
                        changed, authorization, self.job_url, self.proof["profile_id"],
                        self.proof["profile_version"])


if __name__ == "__main__":
    unittest.main()
