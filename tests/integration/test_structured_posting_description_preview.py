"""Structured source HTML remains usable and attributable through cover-letter preview."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.documents.cover_letter_preview import (
    build_cover_letter_preview,
    cover_letter_preview_fingerprint,
)
from services.documents.errors import PreparationError
from tests.integration.test_spine import PROFILE


RAW = {
    "id": 8731,
    "internal_job_id": 8731,
    "title": "Senior Python Engineer",
    "location": {"name": "Remote - United States"},
    "content": (
        "&lt;h2&gt;Responsibilities&lt;/h2&gt;"
        "&lt;ul&gt;&lt;li&gt;Maintain Python services.&lt;/li&gt;"
        "&lt;li&gt;Build SQL reports.&lt;/li&gt;&lt;/ul&gt;"
        "&lt;h2&gt;Qualifications&lt;/h2&gt;"
        "&lt;ul&gt;&lt;li&gt;Maintain current CCNA certification.&lt;/li&gt;&lt;/ul&gt;"
        "&lt;p&gt;Python and SQL experience.&lt;/p&gt;"
        "&lt;script&gt;script-sentinel&lt;/script&gt;"
        "&lt;style&gt;style-sentinel&lt;/style&gt;"
        "&lt;noscript&gt;noscript-sentinel&lt;/noscript&gt;"
    ),
    "updated_at": "2026-10-11T12:00:00Z",
    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/8731",
}
EXPECTED_DESCRIPTION = (
    "Responsibilities\n- Maintain Python services.\n- Build SQL reports.\n"
    "Qualifications\n- Maintain current CCNA certification.\nPython and SQL experience."
)
EXPECTED_RESPONSIBILITIES = ["Maintain Python services.", "Build SQL reports."]


class StructuredPostingDescriptionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-structured-posting-preview-")
        self.store = Store(f"sqlite:///{(Path(self.temporary.name) / 'test.db').as_posix()}")
        self.store.migrate()
        profile = copy.deepcopy(PROFILE)
        profile["facts"] = [
            {
                "id": "python-services",
                "kind": "responsibility",
                "text": "Maintained Python services for customer reporting.",
                "source": "synthetic approved profile fixture",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            },
            {
                "id": "sql-reports",
                "kind": "responsibility",
                "text": "Built SQL reports for customer teams.",
                "source": "synthetic approved profile fixture",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            },
        ]
        self.store.save_profile(profile)
        self.original_raw = copy.deepcopy(RAW)
        self.source_id = configure_source(
            self.store, "example", provider="greenhouse", jitter_seconds=0,
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _transport(self, request):
        self.assertEqual(request.method, "GET")
        if request.url.path == "/v1/boards/example":
            return httpx.Response(200, json={"name": "Example Company"})
        if request.url.path == "/v1/boards/example/jobs":
            return httpx.Response(200, json={"jobs": [self.original_raw], "meta": {"total": 1}})
        self.fail(f"unexpected public source request: {request.url}")

    def test_collect_persist_and_preview_keep_responsibilities_and_raw_provenance(self):
        result = collect_source(
            self.store,
            self.source_id,
            "fixture",
            transport=httpx.MockTransport(self._transport),
        )

        self.assertEqual(result["jobs_seen"], 1)
        saved_job = self.store.jobs("fixture")[0]
        self.assertEqual(saved_job["description"], EXPECTED_DESCRIPTION)
        self.assertNotIn("script-sentinel", saved_job["description"])
        self.assertNotIn("style-sentinel", saved_job["description"])
        self.assertNotIn("noscript-sentinel", saved_job["description"])

        persisted_job = self.store.job(saved_job["id"], profile_id="fixture")
        self.assertEqual(persisted_job["postings"][0]["raw_structured_payload"], self.original_raw)
        self.assertEqual(
            persisted_job["postings"][0]["raw_structured_payload"]["content"],
            self.original_raw["content"],
        )
        with self.store.engine.connect() as connection:
            snapshot = connection.execute(select(m.posting_snapshots)).mappings().one()
        raw_checksum = hashlib.sha256(json.dumps(
            self.original_raw,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(snapshot["raw"], self.original_raw)
        self.assertEqual(snapshot["sha256"], raw_checksum)

        profile = self.store.profile("fixture")
        preview = build_cover_letter_preview(
            profile,
            persisted_job,
            profile["facts"],
            expected_profile_version=profile["version"],
            expected_job_fingerprint=cover_letter_preview_fingerprint(persisted_job),
            length="detailed",
        )
        self.assertEqual(preview["status"], "READY")
        self.assertEqual(preview["job_evidence"]["responsibilities"], EXPECTED_RESPONSIBILITIES)
        self.assertEqual(preview["job_evidence"]["responsibility_source"], "SAVED_DESCRIPTION_PARSED")
        self.assertEqual(set(preview["fact_ids"]), {"python-services", "sql-reports"})
        self.assertNotIn("script-sentinel", preview["preview_text"])
        self.assertNotIn("style-sentinel", preview["preview_text"])
        self.assertNotIn("noscript-sentinel", preview["preview_text"])

    def test_missing_responsibility_heading_still_fails_closed(self):
        profile = self.store.profile("fixture")
        job = {
            "id": "no-responsibility-fixture",
            "title": "Senior Python Engineer",
            "company": "Example Company",
            "skills": ["Python", "SQL"],
            "description": "Qualifications\nPython and SQL experience.",
            "attributes": {},
        }

        with self.assertRaises(PreparationError) as raised:
            build_cover_letter_preview(
                profile,
                job,
                profile["facts"],
                expected_profile_version=profile["version"],
                expected_job_fingerprint=cover_letter_preview_fingerprint(job),
            )

        self.assertEqual(raised.exception.code, "COVER_LETTER_JOB_CONTEXT_MISSING")


if __name__ == "__main__":
    unittest.main()
