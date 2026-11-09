"""Recovered match history cannot certify the fresh profile's unapproved facts."""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.profile_restore import apply_profile_restore
from packages.persistence.store import Store, identity
from services.data_export import export_profile_backup
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.learning.skill_gap_heatmap import get_skill_gap_heatmap
from services.profile_restore import build_restore_plan, preview_profile_restore
from tests.integration.test_spine import PROFILE


class ProfileRestoreMatchCurrentnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="restore-match-currentness-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self._store("source")
        self.target = self._store("target")
        profile = deepcopy(PROFILE)
        profile.update(id="match-source", name="Synthetic Match Source",
                       email="match-source@example.invalid")
        self.source.save_profile(profile)
        self.source_job = capture_manual_job(self.source, {
            "request_id": "restore-approved-match",
            "title": "Python Engineer",
            "company": "Synthetic Match Employer",
            "description": "Required Qualifications:\nPython\nResponsibilities:\nBuild Python services.",
            "reference_url": "https://jobs.example.test/engineering/python",
            "application_url": "https://jobs.example.test/apply/python",
            "profile_id": profile["id"],
        })["job"]
        recalculate(self.source, profile["id"])
        self.backup = export_profile_backup(self.source, profile["id"], output_dir=self.root)
        self.assertEqual(len(self.backup["job_matches"]), 1)
        self.source_match = deepcopy(self.backup["job_matches"][0])
        self.assertEqual(self.source_match["score"], 100)
        self.assertEqual(self.source_match["components"]["requirements"][0]["profile_evidence"],
                         "APPROVED")
        self.profile_id = "match-restored"
        self.plan = build_restore_plan(self.backup, self.profile_id)
        self.request_id = "restore-currentness"
        self.receipt = apply_profile_restore(self.target, self.plan, self.root, self.request_id)

    def _store(self, name):
        store = Store(f"sqlite:///{(self.root / (name + '.db')).as_posix()}")
        self.addCleanup(store.engine.dispose)
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        return store

    def _matches(self, store, profile_id):
        with store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.matches).where(m.matches.c.profile_id == profile_id)
                .order_by(m.matches.c.id)
            ).mappings()]

    def _assert_no_current_assessment(self, store, profile_id):
        profile = store.profile(profile_id)
        self.assertTrue(profile["facts"])
        self.assertTrue(all(not fact["verified"] and not fact["user_approved"]
                            for fact in profile["facts"]))
        self.assertEqual(get_skill_gap_heatmap(store, profile_id), {
            "profile_version": profile["version"], "jobs_examined": 0, "skills": [],
        })
        jobs = store.jobs(profile_id)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["match_score"])
        self.assertIsNone(store.job(jobs[0]["id"], profile_id=profile_id)["match"])
        return jobs[0]

    def _assert_payload_preserved(self, match):
        for field in ("score", "missing", "components"):
            self.assertEqual(match[field], self.source_match[field])

    def test_restored_matches_remain_historical_before_and_after_database_reopen(self):
        job = self._assert_no_current_assessment(self.target, self.profile_id)
        profile = self.target.profile(self.profile_id)
        history = self._matches(self.target, self.profile_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["job_id"], job["id"])
        self.assertNotEqual(history[0]["profile_version"], profile["version"])
        self._assert_payload_preserved(history[0])

        with self.target.engine.connect() as connection:
            versions = connection.execute(select(m.profile_versions).where(
                m.profile_versions.c.profile_id == self.profile_id,
            )).mappings().all()
            archived_facts = connection.execute(select(m.facts).where(
                m.facts.c.profile_id == self.profile_id,
                m.facts.c.version == history[0]["profile_version"],
            )).mappings().all()
        self.assertEqual(len(versions), len(self.backup["profile_versions"]) + 1)
        self.assertIn(history[0]["profile_version"], {row["version"] for row in versions})
        self.assertEqual({row["fact_id"] for row in archived_facts}, {"python", "sql"})
        self.assertTrue(all(not row["verified"] and not row["user_approved"]
                            for row in archived_facts))
        reopened = Store(f"sqlite:///{(self.root / 'target.db').as_posix()}")
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(self._assert_no_current_assessment(reopened, self.profile_id)["id"],
                         job["id"])
        self.assertEqual(self._matches(reopened, self.profile_id), history)

    def test_recalculation_uses_unapproved_current_facts_without_overwriting_imported_history(self):
        job = self._assert_no_current_assessment(self.target, self.profile_id)
        historical = self._matches(self.target, self.profile_id)
        version = self.target.profile(self.profile_id)["version"]

        recalculate(self.target, self.profile_id)

        matches = self._matches(self.target, self.profile_id)
        self.assertEqual(len(matches), 2)
        current = [row for row in matches if row["profile_version"] == version]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["id"], identity(job["id"], self.profile_id, version))
        self.assertEqual(current[0]["score"], 0)
        self.assertEqual(current[0]["missing"], ["Python"])
        self.assertEqual(current[0]["components"]["requirements"][0]["profile_evidence"],
                         "NO_APPROVED_EVIDENCE")
        self.assertEqual([row for row in matches if row["profile_version"] != version], historical)
        heatmap = get_skill_gap_heatmap(self.target, self.profile_id)
        self.assertEqual(heatmap["jobs_examined"], 1)
        self.assertEqual(len(heatmap["skills"]), 1)
        self.assertEqual(heatmap["skills"][0]["approved_evidence_job_count"], 0)
        self.assertEqual(heatmap["skills"][0]["evidence_jobs"][0]["profile_evidence"],
                         ["NO_APPROVED_EVIDENCE"])
        self.assertEqual(len(self.target.jobs(self.profile_id)), 1)

        # A later explicit fact review creates fresh current evidence; it must
        # still preserve the source assessment and the unapproved recalculation.
        reviewed = deepcopy(self.plan["profile"])
        for fact in reviewed["facts"]:
            fact.update(verified=True, user_approved=True, confidence=100)
        self.target.save_profile(reviewed)
        reviewed_version = self.target.profile(self.profile_id)["version"]
        self.assertNotEqual(reviewed_version, version)
        self.assertEqual(get_skill_gap_heatmap(self.target, self.profile_id)["jobs_examined"], 0)
        recalculate(self.target, self.profile_id)
        after_review = self._matches(self.target, self.profile_id)
        self.assertEqual(len(after_review), 3)
        self.assertEqual([row for row in after_review if row["profile_version"] != reviewed_version],
                         matches)
        final_heatmap = get_skill_gap_heatmap(self.target, self.profile_id)
        self.assertEqual(final_heatmap["jobs_examined"], 1)
        self.assertEqual(final_heatmap["skills"][0]["approved_evidence_job_count"], 1)
        self.assertEqual(final_heatmap["skills"][0]["no_approved_evidence_job_count"], 0)

    def test_replay_and_second_restore_preserve_history_without_promoting_it(self):
        before = export_profile_backup(self.target, self.profile_id, output_dir=self.root)
        matches_before = self._matches(self.target, self.profile_id)
        replay = apply_profile_restore(self.target, self.plan, self.root, self.request_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["profile_id"], self.receipt["profile_id"])
        self.assertEqual(export_profile_backup(self.target, self.profile_id, output_dir=self.root), before)
        self.assertEqual(self._matches(self.target, self.profile_id), matches_before)
        self.assertEqual(len(before["jobs"]), 1)
        self.assertIsNone(before["jobs"][0]["match_score"])
        self.assertEqual(len(before["job_matches"]), 1)
        self._assert_payload_preserved(before["job_matches"][0])

        # Exercise the actual backup validator and normalized restore plan;
        # linked historical profile versions must survive repeated recovery.
        preview_profile_restore(before)
        second = self._store("second")
        second_id = "match-restored-again"
        second_plan = build_restore_plan(before, second_id)
        apply_profile_restore(second, second_plan, self.root, "restore-currentness-again")
        self._assert_no_current_assessment(second, second_id)
        second_backup = export_profile_backup(second, second_id, output_dir=self.root)
        preview_profile_restore(second_backup)
        self.assertEqual(len(second_backup["profile_versions"]), len(before["profile_versions"]) + 1)
        self.assertEqual(len(second_backup["job_matches"]), 1)
        self._assert_payload_preserved(second_backup["job_matches"][0])
        self.assertNotEqual(second_backup["job_matches"][0]["profile_version"],
                            second_backup["profile"]["version"])


if __name__ == "__main__":
    unittest.main()
