"""Durable source metadata API/export fidelity, privacy, and profile scope."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.data_export import export_source_metadata_json
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE


_AT = "2026-11-09T03:00:00+00:00"
_EXTENSION = {"novel_rating": 0, "enabled": False, "optional": None,
              "nested": {"labels": ["Développeur", "東京", ""], "empty": {}}, "empty_list": []}


class JobSourceMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'metadata.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.version = self.store.save_profile(deepcopy(PROFILE))
        other = deepcopy(PROFILE)
        other.update(id="other-profile", name="Other Fixture", email="other@example.test")
        self.other_version = self.store.save_profile(other)
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="metadata-company", name="Metadata Fixture", domain="https://company.example.test",
            ))
            for identifier in ("job-one", "job-other", "job-application"):
                connection.execute(m.jobs.insert().values(
                    id=identifier, company_id="metadata-company", requisition=identifier,
                    title="Stored canonical title", description="Python fixture work", skills=["Python"],
                    location="Remote", remote_type="REMOTE", attributes={"provider": "mock-ats"},
                ))
                connection.execute(m.routes.insert().values(
                    id=f"route-{identifier}", job_id=identifier, company_id="metadata-company",
                    provider="mock-ats", tenant="fixture", requisition=identifier,
                    url=f"https://company.example.test/apply/{identifier}",
                ))
            for job_id, profile_id, version in (
                ("job-one", "fixture", self.version), ("job-other", "other-profile", self.other_version),
            ):
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id=profile_id, profile_version=version,
                    score=50, missing=[], components={},
                ))
            connection.execute(m.applications.insert().values(
                id="application-only", job_id="job-application", route_id="route-job-application",
                profile_id="fixture", profile_version=self.version, state="PREPARED", target="REAL",
                fact_ids=[], trace=[], created_at=_AT,
            ))
            self.raw_by_posting = {
                "posting-one": {"title": "Source title", "new_public_metadata": deepcopy(_EXTENSION)},
                "posting-two": {"another_vendor_field": {"industry": "Technology"}},
                "posting-other": {"other_profile_marker": "OTHER_PROFILE_METADATA_CANARY"},
                "posting-application": {"application_only_marker": True},
            }
            for identifier, job_id, source in (
                ("posting-one", "job-one", "source-one"),
                ("posting-two", "job-one", "source-two"),
                ("posting-other", "job-other", "source-other"),
                ("posting-application", "job-application", "source-application"),
            ):
                raw = self.raw_by_posting[identifier]
                connection.execute(m.postings.insert().values(
                    id=identifier, job_id=job_id, source=source, source_job_id=f"native-{identifier}",
                    source_url=f"https://{source}.example.test/jobs", application_url="", raw=raw,
                    fetched_at=_AT,
                ))
                checksum = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                connection.execute(m.posting_snapshots.insert().values(
                    id=f"snapshot-{identifier}", posting_id=identifier, raw=raw, sha256=checksum,
                    retrieved_at=_AT,
                ))
        self.client = self._client()

    def _client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _stored(self):
        with self.store.engine.connect() as connection:
            return {table.name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id),
            ).mappings()] for table in (m.jobs, m.postings, m.posting_snapshots, m.matches, m.applications)}

    def _export(self, profile_id="fixture"):
        return self.client.get(f"/profiles/{profile_id}/exports/source-metadata.json")

    def test_reopen_job_detail_and_export_preserve_unknown_metadata_and_source_identity(self):
        before = self._stored()
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self._client()

        response = self.client.get("/jobs/job-one", params={"profile_id": "fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual(detail["title"], "Stored canonical title")
        postings = {item["id"]: item for item in detail["postings"]}
        self.assertEqual(set(postings), {"posting-one", "posting-two"})
        for identifier, item in postings.items():
            self.assertEqual(item["raw"], self.raw_by_posting[identifier])
            self.assertEqual(item["source_metadata"]["extensions"], self.raw_by_posting[identifier])
            self.assertEqual(item["source_metadata"]["source"]["posting_id"], identifier)
            self.assertEqual(item["source_metadata"]["source"]["source_job_id"], f"native-{identifier}")
            self.assertEqual(item["source_metadata"]["status"], "COMPLETE")

        exported = self._export()
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.headers["cache-control"], "no-store")
        self.assertEqual(exported.headers["content-disposition"],
                         'attachment; filename="mjobs-source-metadata.json"')
        data = exported.json()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["export_type"], "source_metadata")
        envelopes = {item["source_metadata"]["source"]["posting_id"]: item["source_metadata"]
                     for item in data["postings"]}
        self.assertEqual(envelopes["posting-one"]["extensions"]["new_public_metadata"], _EXTENSION)
        self.assertNotEqual(envelopes["posting-one"]["source"], envelopes["posting-two"]["source"])
        self.assertEqual(exported.content, self._export().content)
        self.assertEqual(self._stored(), before)

    def test_export_scopes_match_and_application_jobs_without_other_profile_data(self):
        first = self._export()
        second = self._export("other-profile")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual({row["job_id"] for row in first.json()["postings"]}, {"job-one", "job-application"})
        self.assertEqual({row["job_id"] for row in second.json()["postings"]}, {"job-other"})
        self.assertNotIn("OTHER_PROFILE_METADATA_CANARY", first.text)
        self.assertNotIn("new_public_metadata", second.text)
        self.assertEqual(self._export("missing-profile").status_code, 404)

    def test_nested_private_data_and_embedded_capability_links_are_omitted_explicitly(self):
        raw = deepcopy(self.raw_by_posting["posting-one"])
        raw["vendor"] = {"public": 0, "apiKey": "PRIVATE_METADATA_CANARY"}
        raw["description"] = '<a href="https://example.test/apply?access_token=PRIVATE_METADATA_CANARY">Apply</a>'
        with self.store.engine.begin() as connection:
            connection.execute(m.postings.update().where(m.postings.c.id == "posting-one").values(
                raw=raw, source_job_id="https://example.test/job?token=PRIVATE_METADATA_CANARY",
            ))
        before = self._stored()
        response = self._export()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("PRIVATE_METADATA_CANARY", response.text)
        envelope = next(row["source_metadata"] for row in response.json()["postings"]
                        if row["source_metadata"]["source"]["posting_id"] == "posting-one")
        self.assertEqual(envelope["status"], "PARTIAL")
        self.assertEqual(envelope["omissions"], {"private_fields": 1, "unsafe_urls": 1,
                                                "source_identity_fields": 1})
        self.assertIsNone(envelope["source"]["source_job_id"])
        self.assertIn("SOURCE_IDENTITY_OMITTED", envelope["reason_codes"])
        self.assertEqual(envelope["extensions"]["vendor"], {"public": 0})
        self.assertEqual(self._stored(), before)

    def test_oversized_optional_metadata_keeps_canonical_job_and_returns_explicit_state(self):
        from services.normalization.source_metadata import MAX_STRING_BYTES

        raw = {"large_optional_field": "x" * (MAX_STRING_BYTES + 1)}
        with self.store.engine.begin() as connection:
            connection.execute(m.postings.update().where(m.postings.c.id == "posting-one").values(raw=raw))
        before = self._stored()
        response = self.client.get("/jobs/job-one", params={"profile_id": "fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "Stored canonical title")
        envelope = next(row["source_metadata"] for row in response.json()["postings"]
                        if row["id"] == "posting-one")
        self.assertEqual(envelope["status"], "UNAVAILABLE")
        self.assertEqual(envelope["extensions"], {})
        self.assertIn("METADATA_LIMIT_EXCEEDED", envelope["reason_codes"])
        self.assertEqual(self._export().status_code, 200)
        self.assertEqual(self._stored(), before)

    def test_reopened_export_and_job_metadata_omit_unsafe_url_keys(self):
        marker = "URL_KEY_CANARY"
        unsafe_keys = [
            f"https://jobs.example.test/apply?sig={marker}",
            f"https://person:{marker}@jobs.example.test/apply",
            f"https://jobs.example.test/apply#{marker}",
            f'<a href="https:&#47;&#47;jobs.example.test/apply?sig={marker}">Apply</a>',
        ]
        public_key = "https://jobs.example.test/openings/123"
        raw = {"vendor": {"links_by_url": {
            **{key: {"label": "Apply"} for key in unsafe_keys},
            public_key: {"title": "Développeur", "open": False},
        }}}
        with self.store.engine.begin() as connection:
            connection.execute(m.postings.update().where(m.postings.c.id == "posting-one").values(raw=raw))
        before = self._stored()
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self._client()

        response = self._export()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(marker, response.text)
        exported = next(row["source_metadata"] for row in response.json()["postings"]
                        if row["source_metadata"]["source"]["posting_id"] == "posting-one")
        detail = self.client.get("/jobs/job-one", params={"profile_id": "fixture"})
        self.assertEqual(detail.status_code, 200, detail.text)
        posting = next(row for row in detail.json()["postings"] if row["id"] == "posting-one")
        self.assertEqual(posting["source_metadata"], exported)
        self.assertEqual(exported["extensions"], {"vendor": {"links_by_url": {
            public_key: {"title": "Développeur", "open": False},
        }}})
        self.assertEqual(exported["status"], "PARTIAL")
        self.assertEqual(exported["omissions"], {
            "private_fields": 0, "unsafe_urls": len(unsafe_keys), "source_identity_fields": 0,
        })
        self.assertEqual(exported["reason_codes"], ["UNSAFE_URLS_OMITTED"])
        self.assertEqual(posting["raw"], raw)
        self.assertEqual(self._stored(), before)

    def test_export_is_read_only_and_limits_fail_with_sanitized_errors(self):
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        before = self._stored()
        event.listen(self.store.engine, "before_cursor_execute", capture)
        try:
            with patch("socket.create_connection", side_effect=AssertionError("Unexpected network")):
                self.assertEqual(self._export().status_code, 200)
                with patch("services.data_export.MAX_EXPORT_ROWS", 1):
                    limited = self._export()
                self.assertEqual(limited.status_code, 413)
                self.assertEqual(limited.json(), {"detail": {"code": "DATA_EXPORT_TOO_LARGE"}})
                with patch("services.data_export.MAX_EXPORT_BYTES", 512):
                    self.assertEqual(self._export().status_code, 413)
        finally:
            event.remove(self.store.engine, "before_cursor_execute", capture)
        self.assertTrue(statements)
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql, re.I)
                             for sql in statements), statements)
        self.assertEqual(self._stored(), before)

    def test_export_response_contract_rejects_mismatched_scope_and_versions(self):
        valid = {"schema_version": 1, "export_type": "source_metadata", "profile_id": "fixture", "postings": []}
        for overrides in ({"profile_id": "PRIVATE_OTHER_PROFILE"}, {"schema_version": True},
                          {"schema_version": 2}, {"postings": {}}):
            with self.subTest(overrides=overrides), patch(
                "services.data_export.export_source_metadata_json", return_value=json.dumps({**valid, **overrides}),
            ):
                response = self._export()
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"detail": {"code": "DATA_EXPORT_RESPONSE_INVALID"}})
            self.assertNotIn("PRIVATE_OTHER_PROFILE", response.text)

    def test_existing_adapter_accepts_new_fields_without_adapter_or_schema_changes(self):
        payload = deepcopy(RAW_REMOTIVE_PAYLOAD)
        payload["jobs"] = [payload["jobs"][0]]
        payload["jobs"][0]["new_public_metadata"] = deepcopy(_EXTENSION)
        source_id = configure_source(self.store, "default", provider="remotive", poll_interval=300)
        calls = []

        def transport(request):
            calls.append(str(request.url))
            self.assertEqual(request.url.host, "remotive.com")
            return httpx.Response(200, json=payload)

        result = collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(transport))
        self.assertEqual(result["jobs_seen"], 1)
        self.assertTrue(calls)
        self.store.engine.dispose()
        exported = json.loads(export_source_metadata_json(self.store, "fixture"))
        imported = [row["source_metadata"] for row in exported["postings"]
                    if row["source_metadata"]["extensions"].get("id") == payload["jobs"][0]["id"]]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["extensions"]["new_public_metadata"], _EXTENSION)
        self.assertEqual(imported[0]["status"], "COMPLETE")
        with self.store.engine.connect() as connection:
            raw = connection.execute(select(m.postings.c.raw).where(
                m.postings.c.id == imported[0]["source"]["posting_id"],
            )).scalar_one()
        self.assertEqual(raw, payload["jobs"][0])


if __name__ == "__main__":
    unittest.main()
