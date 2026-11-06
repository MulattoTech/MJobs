"""Metadata fallback through real collection, native evidence and SQLite reopen.

Only HTTP response transport is controlled. No live provider or submission.
"""
import hashlib
import json
import unittest

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.domain_acquisition import native_job_link
from services.discovery.sources.collect import collect_source
from services.discovery.sources.errors import SourceError
from tests.integration import test_official_domain_acquisition as contract
from tests.unit.test_greenhouse_metadata_inventory import OversizeJSON


class GreenhouseMetadataPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.case = contract.OfficialDomainAcquisitionTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.raw.pop("content")
        self.case._native_boundary = self.boundary
        self.meta_total = 1

    def boundary(self, _transport, request):
        self.assertEqual((request.method, request.url.host), ("GET", "boards-api.greenhouse.io"))
        self.case.requests.append(str(request.url))
        if request.url.path == "/v1/boards/example/jobs":
            if request.url.params.get("content") == "true":
                return httpx.Response(200, stream=OversizeJSON(), headers={"content-type": "application/json"})
            return httpx.Response(200, json={"jobs": self.case.inventory, "meta": {"total": self.meta_total}})
        self.assertEqual(request.url.path, "/v1/boards/example")
        return httpx.Response(200, json={"name": "Example Employer"})

    def test_fallback_preserves_exact_native_candidate_snapshot_and_reopen_proof(self):
        request = self.case._collect()
        self.assertEqual(request["state"], "QUEUED")
        self.assertEqual(request["authorization"], "ATS_JOB_LINK_CANDIDATE")
        self.assertEqual(len(self.case.requests), 3)
        checksum = hashlib.sha256(json.dumps(self.case.raw, sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        reopened = Store(self.case.url)
        self.addCleanup(reopened.engine.dispose)
        with reopened.engine.connect() as connection:
            snapshot = connection.execute(select(m.posting_snapshots)).mappings().one()
            candidate = connection.execute(select(m.posting_value_candidates)).mappings().one()
            proof = native_job_link(connection, request)
        self.assertEqual(snapshot["raw"], self.case.raw)
        self.assertEqual(snapshot["sha256"], checksum)
        self.assertEqual(candidate["posting_snapshot_id"], snapshot["id"])
        self.assertEqual(candidate["normalized"]["description"], "")
        self.assertEqual(candidate["normalized"]["attributes"]["description_status"], "UNAVAILABLE")
        self.assertEqual(candidate["normalized"]["attributes"]["skill_evidence"], "title_mentions")
        job = reopened.job(request["trigger_job_id"], profile_id="fixture")
        self.assertEqual(job["match"]["components"]["method"], "weighted_skill_evidence_coverage")
        self.assertEqual(job["match"]["components"]["requirements"][0]["strength"], "UNKNOWN")
        self.assertEqual(proof["snapshot_sha256"], checksum)
        self.assertEqual(proof["native_job_id"], str(self.case.raw["id"]))
        self.assertEqual(proof["website"], self.case.raw["absolute_url"])
        self.assertEqual(proof["profile_version"], reopened.profile("fixture")["version"])
        self.assertEqual(reopened.count("company_domains"), 0)
        self.assertEqual(reopened.count("applications"), 0)

    def test_bad_metadata_count_rejects_before_any_posting_or_snapshot_write(self):
        self.meta_total = 2
        with self.assertRaises(SourceError):
            collect_source(self.case.store, self.case.source_id, "fixture",
                transport=httpx.MockTransport(lambda request: self.boundary(None, request)))
        for table in ("canonical_jobs", "job_postings", "posting_snapshots", "posting_value_candidates"):
            self.assertEqual(self.case.store.count(table), 0, table)
