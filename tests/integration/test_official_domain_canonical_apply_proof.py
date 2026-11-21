"""Bounded reciprocal proof for custom Greenhouse employer job pages.

Provider and public-page responses are controlled at the HTTP request boundary;
the source admission, immutable posting proof, PublicCompanyClient redirect and
robots policy, and official-domain transaction remain the production path.
No external submission or public network request is made.
"""
import hashlib
import json
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.domain_acquisition import acquire_official_domain, native_job_link
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from tests.integration.test_greenhouse import RAW


NATIVE_ID = "8274920"
JOB_REQUEST_URL = f"https://employer.example/jobs/search?gh_jid={NATIVE_ID}"
JOB_FINAL_URL = f"https://employer.example/careers/listing/python-engineer/{NATIVE_ID}?gh_jid={NATIVE_ID}"
JOB_CANONICAL_URL = f"https://employer.example/careers/listing/python-engineer/{NATIVE_ID}"
APPLY_URL = f"https://employer.example/careers/apply/python-engineer/{NATIVE_ID}"
APPLY_IFRAME = (
    "https://job-boards.greenhouse.io/embed/job_app?for=example&token=" + NATIVE_ID
)


def _job_page(*, canonical=JOB_CANONICAL_URL, job_url=JOB_CANONICAL_URL,
              job_id=NATIVE_ID, title="Python Engineer", company="Example Employer",
              apply_href=APPLY_URL):
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "identifier": {"@type": "PropertyValue", "name": "Example Employer", "value": job_id},
        "title": title,
        "url": job_url,
        "hiringOrganization": {"@type": "Organization", "name": company,
                               "sameAs": "https://employer.example"},
    }
    return (
        f'<link rel="canonical" href="{canonical}">'
        '<script type="application/ld+json">' + json.dumps(posting) + '</script>'
        f'<a href="{apply_href}">Apply</a>'
    )


def _apply_page(*, iframe_src=APPLY_IFRAME):
    return f'<iframe src="{iframe_src}" title="Greenhouse application"></iframe>'


class OfficialDomainCanonicalApplyProofTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fully initialized synthetic acquisition contract,
        # without changing its files or running its test methods.
        from tests.integration.test_official_domain_acquisition import OfficialDomainAcquisitionTests

        self.case = OfficialDomainAcquisitionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        self.case.raw = dict(RAW, id=int(NATIVE_ID), absolute_url=JOB_REQUEST_URL)
        self.case.inventory = [self.case.raw]
        self.case.search_profile_id = None
        self.request = self.case._collect()
        with self.case.store.engine.connect() as connection:
            row = connection.execute(select(m.company_discovery_requests)).mappings().one()
            proof = native_job_link(connection, row)
        self.assertIsNotNone(proof)
        self.proof = proof
        self.authorization = {
            "request_id": self.request["id"],
            "company_id": self.request["company_id"],
            "native_job_link": proof,
        }
        self.urls = []
        self.robots = "User-agent: *\nAllow: /"
        self.job_html = _job_page()
        self.apply_html = _apply_page()
        self.final_redirect = None
        self.apply_redirect = None

    def _request_once(self, url, _deadline):
        self.urls.append(url)
        request = httpx.Request("GET", url)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots, request=request)
        if path == "/jobs/search":
            return httpx.Response(307, headers={
                "location": "/careers/search/redirect?gh_jid=" + NATIVE_ID,
            }, request=request)
        if path == "/careers/search/redirect":
            return httpx.Response(307, headers={
                "location": self.final_redirect or (
                    "/careers/listing/python-engineer/" + NATIVE_ID + "?gh_jid=" + NATIVE_ID
                ),
            }, request=request)
        if path == f"/careers/listing/python-engineer/{NATIVE_ID}":
            return httpx.Response(200, text=self.job_html, request=request)
        if path == f"/careers/apply/python-engineer/{NATIVE_ID}":
            if self.apply_redirect:
                return httpx.Response(302, headers={"location": self.apply_redirect}, request=request)
            return httpx.Response(200, text=self.apply_html, request=request)
        return httpx.Response(404, request=request)

    def _acquire(self):
        client = PublicCompanyClient(self.proof["website"])
        self.addCleanup(client.close)
        with patch.object(PublicCompanyClient, "_request_once", self._request_once), \
             patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0):
            return acquire_official_domain(
                self.case.store, self.authorization, client, lambda: None,
                lambda _connection: self.authorization,
            )

    def test_custom_page_canonical_and_native_apply_frame_verify_bound_domain(self):
        response = self._acquire()

        self.assertEqual(response.url, JOB_FINAL_URL)
        self.assertEqual(self.urls.count(APPLY_URL), 1)
        self.assertEqual(self.case.store.count("company_domains"), 1)
        with self.case.store.engine.connect() as connection:
            domain = connection.execute(select(m.company_domains)).mappings().one()
            self.assertEqual(domain["domain"], "employer.example")
            self.assertTrue(domain["verified"])
            self.assertEqual(domain["verification_method"], "FIRST_PARTY_JOB_LINK")
            self.assertEqual(domain["company_id"], self.request["company_id"])
            self.assertIn(self.proof["snapshot_sha256"], " ".join(domain["evidence"]))
            self.assertIn(hashlib.sha256(self.job_html.encode()).hexdigest(),
                          " ".join(domain["evidence"]))
            self.assertIn(hashlib.sha256(self.apply_html.encode()).hexdigest(),
                          " ".join(domain["evidence"]))
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == "COMPANY_DOMAIN_ACQUIRED")).scalar_one()
        self.assertEqual(audit.get("apply_response_sha256"),
                         hashlib.sha256(self.apply_html.encode()).hexdigest())

    def test_wrong_native_iframe_token_does_not_verify_domain(self):
        self.apply_html = _apply_page(iframe_src=APPLY_IFRAME.replace(NATIVE_ID, "9999999"))
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_wrong_native_iframe_tenant_does_not_verify_domain(self):
        self.apply_html = _apply_page(iframe_src=APPLY_IFRAME.replace("for=example", "for=other"))
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_duplicate_native_iframe_parameter_does_not_verify_domain(self):
        self.apply_html = _apply_page(iframe_src=APPLY_IFRAME + "&token=" + NATIVE_ID)
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_cross_origin_apply_link_does_not_verify_domain(self):
        self.job_html = _job_page(
            apply_href=f"https://other.example/careers/apply/python-engineer/{NATIVE_ID}"
        )
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertNotIn(APPLY_URL, self.urls)
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_ambiguous_same_origin_apply_routes_do_not_verify_domain(self):
        self.job_html = _job_page() + (
            f'<a href="/careers/apply/alternate-python-engineer/{NATIVE_ID}">Apply</a>'
        )
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertNotIn(APPLY_URL, self.urls)
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_cross_origin_apply_redirect_is_blocked(self):
        self.apply_redirect = f"https://other.example/apply/{NATIVE_ID}"
        with self.assertRaisesRegex(PublicFetchError, "REDIRECT_BLOCKED"):
            self._acquire()
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_canonical_must_match_final_path_and_exact_native_query(self):
        self.job_html = _job_page(
            canonical=f"https://employer.example/careers/listing/other/{NATIVE_ID}"
        )
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertNotIn(APPLY_URL, self.urls)
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_final_redirect_query_must_be_exact_native_id(self):
        self.final_redirect = (
            f"/careers/listing/python-engineer/{NATIVE_ID}?gh_jid={NATIVE_ID}&gh_jid={NATIVE_ID}"
        )
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertNotIn(APPLY_URL, self.urls)
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_jobposting_identifier_must_match_before_apply_fetch(self):
        self.job_html = _job_page(job_id="9999999")
        with self.assertRaisesRegex(PublicFetchError, "COMPANY_DOMAIN_CORROBORATION_REQUIRED"):
            self._acquire()
        self.assertNotIn(APPLY_URL, self.urls)
        self.assertEqual(self.case.store.count("company_domains"), 0)

    def test_robots_denial_on_same_origin_apply_page_does_not_verify_domain(self):
        self.robots = "User-agent: *\nDisallow: /careers/apply/"
        with self.assertRaisesRegex(PublicFetchError, "ROBOTS_DENIED"):
            self._acquire()
        self.assertEqual(self.case.store.count("company_domains"), 0)
