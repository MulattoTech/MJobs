"""Profile-scoped company notes use append-only audit history."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.audit_history import record_company_discovered
from services.company_discovery.sources import upsert_company_job_source
from services.tracking.companies import read_company_tracking
from services.company_discovery.sources import list_company_source_view
from tests.integration.test_spine import PROFILE


class CompanyTrackingApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-company-tracking-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'tracking.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})
        self.store.save_profile({**PROFILE, "id": "unrelated", "name": "Unrelated Profile",
                                "email": "unrelated@example.invalid"})
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "acme", "name": "Acme", "domain": ""})
            upsert(connection, m.companies, {"id": "outside", "name": "Outside", "domain": ""})
            record_company_discovered(connection, "fixture", "acme")
            record_company_discovered(connection, "other", "acme")
            record_company_discovered(connection, "other", "outside")
        self.source = upsert_company_job_source(self.store, company_id="acme",
            provider="rss", tenant_identifier="acme", career_url="https://fixture.invalid/careers",
            collection_strategy="RSS", confidence=0.9, discovered_automatically=True)
        self.outside_source = upsert_company_job_source(self.store, company_id="outside",
            provider="rss", tenant_identifier="outside", career_url="https://outside.invalid/careers",
            collection_strategy="RSS", confidence=0.9, discovered_automatically=True)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_company_notes_are_profile_scoped_idempotent_and_durable(self):
        url = "/profiles/fixture/companies/acme/tracking"
        other_url = "/profiles/other/companies/acme/tracking"
        self.assertEqual(self.client.get(url).json(), {"notes": [], "history": []})
        payload = {"text": "Ask about on-call rotation.", "request_id": "note-1"}
        created = self.client.post(url + "/notes", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["notes"][0]["text"], payload["text"])
        replay = self.client.post(url + "/notes", json=payload)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), created.json())
        self.assertEqual(self.client.post(url + "/notes", json={
            **payload, "text": "Conflicting replay."}).status_code, 409)
        self.assertEqual(self.client.get(other_url).json()["notes"], [])
        unrelated = self.client.get("/profiles/unrelated/companies/acme/tracking")
        self.assertEqual(unrelated.status_code, 404)
        self.assertEqual(self.client.post("/profiles/unrelated/companies/acme/tracking/notes",
                                          json={"text": "Should not write", "request_id": "foreign"}).status_code, 404)
        self.assertEqual(self.client.get("/profiles/fixture/companies/outside/tracking").status_code, 404)
        self.assertEqual(self.client.get("/profiles/missing/companies/acme/tracking").status_code, 404)
        view = list_company_source_view(self.store, "fixture")
        rows = {row["company_id"]: row for row in view}
        self.assertEqual(set(rows), {"acme", "outside"})
        self.assertEqual(rows["acme"]["profile_id"], "fixture")
        self.assertTrue(rows["acme"]["profile_company_associated"])
        self.assertFalse(rows["outside"]["profile_company_associated"])
        self.assertEqual(read_company_tracking(self.store, "fixture", "acme"), created.json())
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.action == "COMPANY_NOTE")).mappings())
        self.assertEqual(len(events), 1)

    def test_note_validation_and_concurrent_distinct_writes(self):
        url = "/profiles/fixture/companies/acme/tracking/notes"
        for text in ("   ", "x" * 4001):
            response = self.client.post(url, json={"text": text, "request_id": "invalid"})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.post(url, json={"text": "ok", "request_id": "  bad"}).status_code, 422)

        def append(index):
            return self.client.post(url, json={"text": f"Concurrent note {index}",
                                               "request_id": f"parallel-{index}"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(append, range(8)))
        self.assertTrue(all(response.status_code == 201 for response in responses),
                        [response.text for response in responses])
        tracking = self.client.get("/profiles/fixture/companies/acme/tracking").json()
        self.assertEqual(len(tracking["notes"]), 8)
        self.assertEqual(sorted(note["sequence"] for note in tracking["notes"]), list(range(1, 9)))
