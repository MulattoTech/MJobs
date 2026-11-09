"""Real source ingestion must bind promoted fields to durable posting evidence."""
from copy import deepcopy
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.ai.input_freshness import job_fingerprint
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from services.dedupe.source_values import record_candidate, resolve_job_values
from services.discovery.company_feed import import_company_feed_jobs
from services.discovery.job_risk_signals import evaluate_job_risk
from services.documents.generate import prepare
from tests.integration import test_source_value_resolution as source_value_resolution


class CanonicalFieldProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_value_resolution.SourceValueResolutionTests()
        self.fixture.setUp()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.tearDown()

    def _job(self):
        return self.store.job(self.fixture.job_id, "fixture")

    def _selected(self, job):
        selected_id = job["attributes"]["source_resolution"]["selected_candidate_id"]
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == selected_id)).mappings().one())

    def _collect(self, **overrides):
        row = {
            "guid": "same-role", "title": "Python Engineer", "link": "/jobs/one",
            "description": "Python engineering. Salary $90,000 - $110,000 per year.",
            "location": "Remote - United States", **overrides,
        }
        source = self.fixture.rich
        return import_company_feed_jobs(
            self.store, company_id="company", source_id=source["id"],
            provider="rss", tenant="company", endpoint=source["endpoint"],
            strategy="RSS", items=[row], profile_id="fixture", fetch_available=True,
        )

    def test_import_binds_promoted_fields_to_durable_candidate_and_snapshot(self):
        extra = {"rating": 4.6, "future_detail": ["preserve", {"enabled": True}]}
        self._collect(future_metadata=extra)
        job = self._job()
        candidate = self._selected(job)
        resolution = job["attributes"]["source_resolution"]
        self.assertEqual(resolution["field_provenance_schema_version"], 1)
        evidence = resolution["field_provenance"]
        for field, normalized_field in (
            ("title", "title"), ("description", "description"), ("skills", "skills"),
            ("location", "location"), ("remote_type", "remote_type"),
            ("salary_min", "attributes.salary_min"),
            ("normalized_title", "attributes.normalized_title"),
        ):
            with self.subTest(field=field):
                item = evidence[field]
                self.assertEqual(item["source"], "rich.example")
                self.assertEqual(item["source_endpoint"], self.fixture.rich["endpoint"])
                self.assertEqual(item["provider"], "rss")
                self.assertEqual(item["source_authority"], "UNKNOWN")
                self.assertIsNone(item["confidence"])
                self.assertEqual(item["basis"], "NORMALIZED_CANDIDATE")
                self.assertEqual(item["normalized_field"], normalized_field)
                for name in ("posting_id", "posting_snapshot_id", "company_source_id",
                             "quality_observation_id", "observed_at"):
                    self.assertEqual(item[name], candidate[name])
                self.assertEqual(item["candidate_id"], candidate["id"])
        for alias, source_field, derivation in (
            ("description_text", "description", "CANONICAL_DESCRIPTION_TEXT"),
            ("location_text", "location", "CANONICAL_LOCATION_TEXT"),
        ):
            for key in ("source", "candidate_id", "posting_id", "posting_snapshot_id",
                        "normalized_field", "confidence"):
                self.assertEqual(evidence[alias][key], evidence[source_field][key])
            self.assertEqual(evidence[alias]["derivation"], derivation)
        for local_field in ("first_seen_at", "last_seen_at", "last_collected_at", "lifecycle"):
            self.assertNotIn(local_field, evidence)
        self.assertNotIn("field_provenance", candidate["normalized"]["attributes"])
        self.assertNotIn("source_resolution", candidate["normalized"]["attributes"])
        with self.store.engine.connect() as connection:
            snapshot = connection.execute(select(m.posting_snapshots).where(
                m.posting_snapshots.c.id == candidate["posting_snapshot_id"])).mappings().one()
            posting = connection.execute(select(m.postings).where(
                m.postings.c.id == candidate["posting_id"])).mappings().one()
        self.assertEqual(snapshot["posting_id"], posting["id"])
        self.assertEqual(snapshot["raw"], posting["raw"])
        self.assertEqual(snapshot["raw"]["future_metadata"], extra)

        reopened = Store(str(self.store.engine.url))
        try:
            persisted = reopened.job(self.fixture.job_id)
            self.assertEqual(persisted["attributes"]["source_resolution"], resolution)
            self.assertEqual(persisted["postings"][0]["raw"]["future_metadata"], extra)
        finally:
            reopened.engine.dispose()

    def test_refresh_rebinds_fields_without_rewriting_prior_evidence(self):
        self.fixture.collect(self.fixture.rich, True)
        original = self._job()
        old_candidate = self._selected(original)
        old_evidence = original["attributes"]["source_resolution"]["field_provenance"]["title"]
        with self.store.engine.connect() as connection:
            old_snapshot = dict(connection.execute(select(m.posting_snapshots).where(
                m.posting_snapshots.c.id == old_candidate["posting_snapshot_id"])).mappings().one())

        self.fixture.collect(self.fixture.rich, True, title="Updated Python role")
        updated = self._job()
        new_candidate = self._selected(updated)
        new_evidence = updated["attributes"]["source_resolution"]["field_provenance"]["title"]
        self.assertEqual(updated["title"], "Updated Python role")
        self.assertNotEqual(new_evidence["candidate_id"], old_evidence["candidate_id"])
        self.assertNotEqual(new_evidence["posting_snapshot_id"], old_evidence["posting_snapshot_id"])
        self.assertEqual(new_evidence["posting_id"], old_evidence["posting_id"])
        self.assertEqual(new_evidence["candidate_id"], new_candidate["id"])
        with self.store.engine.connect() as connection:
            retained = dict(connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == old_candidate["id"])).mappings().one())
            retained_snapshot = dict(connection.execute(select(m.posting_snapshots).where(
                m.posting_snapshots.c.id == old_snapshot["id"])).mappings().one())
        self.assertEqual(retained, old_candidate)
        self.assertEqual(retained_snapshot, old_snapshot)
        self.assertEqual(len(updated["postings"][0]["snapshots"]), 2)

    def test_verified_sparse_winner_does_not_borrow_richer_fields_or_confidence(self):
        record_company_domain(self.store, "company", "sparse.example", "FIRST_PARTY_JOB_LINK",
                              ["Synthetic independent first-party evidence"])
        self.fixture.collect(self.fixture.rich, True)
        self.fixture.collect(self.fixture.sparse, False)
        job = self._job()
        winner = self._selected(job)
        resolution = job["attributes"]["source_resolution"]
        self.assertEqual(job["title"], "Sparse role")
        self.assertEqual(job["description"], "")
        self.assertIsNone(job["salary_min"])
        self.assertEqual(resolution["authority"], "VERIFIED_EMPLOYER")
        for field in ("title", "description", "salary_min"):
            with self.subTest(field=field):
                evidence = resolution["field_provenance"][field]
                self.assertEqual(evidence["source"], "sparse.example")
                self.assertEqual(evidence["candidate_id"], winner["id"])
                self.assertEqual(evidence["company_source_id"], self.fixture.sparse["id"])
                self.assertEqual(evidence["source_authority"], "VERIFIED_EMPLOYER")
                self.assertIsNone(evidence["confidence"])
        self.assertEqual(len(job["postings"]), 2)

    def test_source_claims_are_stripped_at_capture_and_from_legacy_candidates(self):
        self.fixture.collect(self.fixture.rich, True)
        candidate = self._selected(self._job())
        forged = deepcopy(candidate["normalized"])
        forged["attributes"]["field_provenance"] = {
            "description": {"source": "FORGED_VERIFIED_SOURCE", "confidence": 1.0},
        }
        forged["attributes"]["source_resolution"] = {
            "selected_candidate_id": "forged", "field_provenance_schema_version": 1,
            "field_provenance": {"description": {"source": "FORGED", "confidence": 1.0}},
        }
        input_before = deepcopy(forged)
        with self.store.engine.begin() as connection:
            captured_id = record_candidate(
                connection, posting_id=candidate["posting_id"],
                company_source_id=candidate["company_source_id"],
                observation_id=candidate["quality_observation_id"], normalized=forged,
            )
            captured = connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == captured_id)).mappings().one()
        self.assertEqual(forged, input_before)
        self.assertEqual(captured_id, candidate["id"])
        self.assertNotIn("field_provenance", captured["normalized"]["attributes"])
        self.assertNotIn("source_resolution", captured["normalized"]["attributes"])

        # Simulate a stored candidate written before capture-time filtering existed.
        for normalized_title_state in ("missing", "empty", "null"):
            with self.subTest(normalized_title=normalized_title_state):
                legacy = deepcopy(forged)
                if normalized_title_state == "missing":
                    legacy["attributes"].pop("normalized_title", None)
                else:
                    legacy["attributes"]["normalized_title"] = (
                        "" if normalized_title_state == "empty" else None
                    )
                reference = now()
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.posting_value_candidates).where(
                        m.posting_value_candidates.c.id == candidate["id"]).values(normalized=legacy))
                    resolve_job_values(connection, self.fixture.job_id, at=reference, seen_at=reference)
                job = self._job()
                self.assertNotIn("field_provenance", job["attributes"])
                resolution = job["attributes"]["source_resolution"]
                self.assertEqual(resolution["selected_candidate_id"], candidate["id"])
                evidence = resolution["field_provenance"]["description"]
                self.assertEqual(evidence["source"], "rich.example")
                self.assertEqual(evidence["source_authority"], "UNKNOWN")
                self.assertIsNone(evidence["confidence"])
                self.assertEqual(job["normalized_title"], job["title"])
                title_evidence = resolution["field_provenance"]["normalized_title"]
                self.assertEqual(title_evidence["normalized_field"], "title")
                self.assertEqual(title_evidence["candidate_id"], candidate["id"])
                self.assertEqual(title_evidence["derivation"], "CANONICAL_TITLE_FALLBACK")

    def test_retargeted_portal_keeps_historical_origin_and_authority(self):
        self.fixture.collect(self.fixture.rich, True)
        candidate = self._selected(self._job())
        old_endpoint = self.fixture.rich["endpoint"]
        record_company_domain(self.store, "company", "new.example", "FIRST_PARTY_JOB_LINK",
                              ["Synthetic verified replacement portal"])
        retargeted = upsert_company_job_source(
            self.store, company_id="company", provider="rss", tenant_identifier="company",
            career_url="https://rich.example/careers", feed_url="https://new.example/jobs.xml",
            collection_strategy="RSS", discovered_automatically=True,
        )
        self.assertEqual(retargeted["id"], self.fixture.rich["id"])
        reference = now()
        with self.store.engine.begin() as connection:
            resolve_job_values(connection, self.fixture.job_id, at=reference, seen_at=reference)
        resolution = self._job()["attributes"]["source_resolution"]
        evidence = resolution["field_provenance"]["description"]
        self.assertEqual(resolution["authority"], "UNKNOWN")
        self.assertEqual(evidence["source_authority"], "UNKNOWN")
        self.assertEqual(evidence["source"], "rich.example")
        self.assertEqual(evidence["source_endpoint"], old_endpoint)
        self.assertEqual(evidence["candidate_id"], candidate["id"])
        self.assertEqual(evidence["posting_snapshot_id"], candidate["posting_snapshot_id"])

    def test_risk_consumer_uses_persisted_attribution_without_changing_fact_freshness(self):
        self._collect(description="Python engineering. An application fee of $200 is required.")
        job = self._job()
        result = evaluate_job_risk(job)
        payment = result["signals_by_code"]["PAYMENT_REQUEST"]
        self.assertEqual(payment["state"], "WARN")
        evidence = payment["warning"]["evidence"]
        self.assertEqual(evidence["field"], "description")
        self.assertEqual(evidence["source"], "rich.example")
        self.assertIsNone(evidence["confidence"])

        # Only nested attribution changes; all job facts and collection times stay fixed.
        amended = deepcopy(job)
        amended["attributes"]["source_resolution"]["field_provenance"]["description"].update(
            observed_at="2030-01-01T00:00:00+00:00", posting_snapshot_id="another-snapshot",
        )
        self.assertEqual(job_fingerprint(amended), job_fingerprint(job))
        amended["description"] += " A materially different job obligation."
        self.assertNotEqual(job_fingerprint(amended), job_fingerprint(job))

    def test_selected_listing_url_provenance_does_not_relabel_frozen_application_route(self):
        self.fixture.collect(self.fixture.rich, True)
        application_id = prepare(
            self.store, self.fixture.job_id, "fixture", self.fixture.root / "docs",
        )
        old_url = self._job()["application_url"]
        self.fixture.collect(self.fixture.rich, True, link="/jobs/revised")
        current = self._job()
        resolution = current["attributes"]["source_resolution"]
        candidate = self._selected(current)
        evidence = resolution["field_provenance"]["application_url"]
        self.assertTrue(resolution["route_review_required"])
        self.assertEqual(resolution["preserved_route_url"], old_url)
        self.assertNotEqual(current["application_url"], old_url)
        self.assertEqual(candidate["application_url"], current["application_url"])
        self.assertEqual(evidence["basis"], "SELECTED_LISTING_URL")
        self.assertEqual(evidence["candidate_field"], "application_url")
        self.assertEqual(evidence["candidate_id"], candidate["id"])
        self.assertNotIn("normalized_field", evidence)
        frozen = next(row for row in self.store.applications() if row["id"] == application_id)
        self.assertEqual(frozen["application_url"], old_url)


if __name__ == "__main__":
    unittest.main()
