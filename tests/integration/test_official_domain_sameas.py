"""Synthetic public acquisition with scalar JobPosting sameAs employer evidence.

Compose the existing API/operation contract without changing its owned fixture.
Only external response boundaries are controlled; live providers and RQ are not
covered. No verified employer domain is seeded and no application is submitted.
"""
import json
import unittest

from tests.integration import test_official_domain_acquisition as contract


class OfficialDomainSameAsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.case = contract.OfficialDomainAcquisitionTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        start, body = self.case.page.split('<script type="application/ld+json">', 1)
        structured, end = body.split('</script>', 1)
        node = json.loads(structured)
        del node['hiringOrganization']['url']
        node['hiringOrganization']['sameAs'] = 'https://employer.example/'
        self.case.page = start + '<script type="application/ld+json">' + json.dumps(node) + '</script>' + end

    def test_sameas_enrollment_persists_through_refresh_reopen_and_replay(self):
        # This exercises real source intake, outbox operation authorization,
        # reciprocal acquisition, source enrollment, scheduler refresh and API
        # readback after reopening SQLite; it also proves no application attempt.
        self.case.test_reciprocal_native_job_page_enrolls_and_survives_reopen_and_replay()

    def test_sameas_without_reciprocal_link_does_not_verify_domain(self):
        self.case.test_self_asserted_jobposting_without_reciprocal_infrastructure_stays_unverified()

    def test_sameas_source_revocation_during_fetch_prevents_promotion(self):
        self.case.test_source_revocation_during_candidate_response_prevents_promotion()

    def test_sameas_domain_revocation_blocks_refresh_before_egress(self):
        self.case.test_revoked_acquired_domain_blocks_subsequent_native_refresh_before_egress()
