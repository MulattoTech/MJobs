"""Synthetic source probes must prove the selected employer and job identity."""
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import unittest

from cli.verification_checks import _corroborate
from cli.verification_fixture import validate_registry_probe
from services.discovery.sources.contract import SearchBatch, SourceCompany, SourceItem
from services.discovery.sources.greenhouse import Greenhouse


class RegistryProbeTests(unittest.TestCase):
    @staticmethod
    def batch_for(adapter):
        company = SourceCompany(
            ("greenhouse", adapter.tenant),
            "Synthetic employer",
            "https://job-boards.greenhouse.io/" + adapter.tenant,
        )
        item = SourceItem({"id": 288, "title": "Synthetic role"}, company)
        return SearchBatch(
            "https://boards-api.greenhouse.io/v1/boards/" + adapter.tenant + "/jobs",
            (item,), company, complete_inventory=True,
        )

    def test_registry_probe_rejects_otherwise_valid_job_from_wrong_tenant(self):
        adapter = Greenhouse("other-tenant")
        batch = self.batch_for(adapter)

        with self.assertRaisesRegex(RuntimeError, "source identity mismatch"):
            validate_registry_probe(
                adapter,
                batch,
                ["/v1/boards/fixture", "/v1/boards/fixture/jobs"],
            )

    def test_registry_probe_accepts_expected_fixture_identity_and_attribution(self):
        adapter = Greenhouse("fixture")
        batch = self.batch_for(adapter)

        validate_registry_probe(
            adapter,
            batch,
            ["/v1/boards/fixture", "/v1/boards/fixture/jobs"],
        )

    def test_parent_rejects_mutated_registry_tenant_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "fixture"
            workspace.mkdir()
            with closing(sqlite3.connect(workspace / "fixture.db")) as db:
                db.execute("CREATE TABLE job_postings (id INTEGER)")
                db.executemany("INSERT INTO job_postings VALUES (?)", [(1,), (2,), (3,), (4,)])
                db.commit()
            expected = {"invocation_id": "test", "workspace": str(workspace)}
            report = {
                "workspace": str(workspace),
                "checks": {
                    "adapter registry": {"status": "PASS"},
                    "mock global source": {"status": "PASS"},
                },
                "observations": {
                    "registry_requests": ["/v1/boards/fixture", "/v1/boards/fixture/jobs"],
                    "registry_company": "Synthetic employer",
                    "registry_job_requisition": "288",
                    "registry_provider": "greenhouse",
                    "registry_tenant": "wrong-tenant",
                    "profile_id": "verification-test",
                    "canonical_jobs": 2,
                },
            }

            with self.assertRaisesRegex(ValueError, "registry source identity corroboration"):
                _corroborate(report, expected)


if __name__ == "__main__":
    unittest.main()
