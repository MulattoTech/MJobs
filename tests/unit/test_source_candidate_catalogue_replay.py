"""A replay is idempotent while distinct source observations remain durable."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.source_candidates.catalogue import (
    SourceCandidateConflict,
    record_capture,
)


class SourceCandidateCatalogueReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-source-candidate-replay-")
        self.store = Store(
            f"sqlite:///{(Path(self.temp.name) / 'catalogue.db').as_posix()}"
        )
        self.store.migrate()
        for profile_id in ("fixture", "other"):
            self.store.save_profile({
                "id": profile_id,
                "name": f"{profile_id} Profile",
                "email": f"{profile_id}@example.invalid",
                "facts": [],
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _capture(self, *, request_id="capture-1", profile_id="fixture",
                 url="https://tenant.board.example/jobs/role-1",
                 origin="LinkedIn", hops=None):
        with self.store.engine.begin() as connection:
            return record_capture(
                connection,
                profile_id=profile_id,
                request_id=request_id,
                job_id=None,
                application_id=None,
                discovery_origin=origin,
                source_url=None,
                application_url=url,
                observed_hops=hops,
                capture_method="MANUAL_CAPTURE",
            )

    def _candidate(self, candidate_id):
        with self.store.engine.connect() as connection:
            candidate = connection.execute(select(m.source_candidates).where(
                m.source_candidates.c.id == candidate_id,
            )).mappings().one()
            observations = connection.execute(select(
                m.source_candidate_observations,
            ).where(
                m.source_candidate_observations.c.candidate_id == candidate_id,
            )).mappings().all()
        return dict(candidate), [dict(row) for row in observations]

    def test_exact_capture_replay_preserves_timestamps_and_single_evidence_row(self):
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-01T00:00:00+00:00"):
            candidate_id = self._capture(hops=["https://referrer.example/path"])
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-02T00:00:00+00:00"):
            replayed_id = self._capture(hops=["https://referrer.example/path"])

        candidate, observations = self._candidate(candidate_id)
        self.assertEqual(replayed_id, candidate_id)
        self.assertEqual(candidate["first_seen_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["observed_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(observations[0]["discovery_origin"], "LinkedIn")
        self.assertEqual(observations[0]["origin_attribution"], "USER_REPORTED")
        self.assertEqual(observations[0]["observed_hops"], [
            "https://referrer.example/path",
        ])

    def test_new_event_advances_last_seen_without_rewriting_prior_evidence(self):
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-01T00:00:00+00:00"):
            candidate_id = self._capture(request_id="first-event")
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-03T00:00:00+00:00"):
            second_candidate_id = self._capture(request_id="second-event", origin=None)

        candidate, observations = self._candidate(candidate_id)
        self.assertEqual(second_candidate_id, candidate_id)
        self.assertEqual(candidate["first_seen_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-03T00:00:00+00:00")
        self.assertEqual(len(observations), 2)
        by_key = {row["capture_key"]: row for row in observations}
        self.assertEqual(by_key["MANUAL_CAPTURE:first-event"]["observed_at"],
                         "2026-10-01T00:00:00+00:00")
        self.assertEqual(by_key["MANUAL_CAPTURE:second-event"]["observed_at"],
                         "2026-10-03T00:00:00+00:00")
        self.assertIsNone(by_key["MANUAL_CAPTURE:second-event"]["discovery_origin"])
        self.assertEqual(by_key["MANUAL_CAPTURE:second-event"]["origin_attribution"], "UNKNOWN")
        self.assertEqual(by_key["MANUAL_CAPTURE:second-event"]["evidence_status"], "USER_REPORTED")
        self.assertEqual(by_key["MANUAL_CAPTURE:second-event"]["observed_hops"], [
            {"state": "UNKNOWN"},
        ])

    def test_out_of_order_new_event_keeps_minimum_first_and_maximum_last_seen(self):
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-03T00:00:00+00:00"):
            candidate_id = self._capture(request_id="later-event")
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-01T00:00:00+00:00"):
            earlier_id = self._capture(request_id="earlier-event", origin=None)

        candidate, observations = self._candidate(candidate_id)
        self.assertEqual(earlier_id, candidate_id)
        self.assertEqual(candidate["first_seen_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-03T00:00:00+00:00")
        self.assertEqual(len(observations), 2)

    def test_conflicting_replay_does_not_change_candidate_or_original_evidence(self):
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-01T00:00:00+00:00"):
            candidate_id = self._capture(hops=["https://referrer.example/path"])

        with patch("services.source_candidates.catalogue.now", return_value="2026-10-04T00:00:00+00:00"):
            with self.assertRaises(SourceCandidateConflict):
                self._capture(origin="Indeed", hops=["https://changed.example/path"])

        candidate, observations = self._candidate(candidate_id)
        self.assertEqual(candidate["last_seen_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["discovery_origin"], "LinkedIn")
        self.assertEqual(observations[0]["observed_hops"], [
            "https://referrer.example/path",
        ])

    def test_profile_and_provider_tenant_boundaries_are_not_merged(self):
        with patch("services.source_candidates.catalogue.now", return_value="2026-10-01T00:00:00+00:00"):
            tenant_a = self._capture(request_id="tenant-a", url="https://a.board.example/job")
            tenant_b = self._capture(request_id="tenant-b", url="https://b.board.example/job")
            other_profile = self._capture(
                request_id="other-profile", profile_id="other",
                url="https://a.board.example/job",
            )

        self.assertEqual(len({tenant_a, tenant_b, other_profile}), 3)
        other_candidate, other_observations = self._candidate(other_profile)
        self.assertEqual(other_candidate["profile_id"], "other")
        self.assertEqual(len(other_observations), 1)


if __name__ == "__main__":
    unittest.main()
