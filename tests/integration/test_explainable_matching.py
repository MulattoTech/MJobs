"""Deterministic match explanations preserve requirement uncertainty."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packages.persistence.store import Store
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


class ExplainableMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'matching.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = copy.deepcopy(PROFILE)
        profile["facts"].append({
            "id": "approved-ad", "kind": "skill", "text": "Administered Active Directory in enterprise environments",
            "source": "user-confirmed test fact", "confidence": 100,
            "verified": True, "user_approved": True,
        })
        profile["facts"].append({
            "id": "pending-server", "kind": "skill", "text": "Windows Server",
            "source": "imported, not reviewed", "confidence": 100,
            "verified": False, "user_approved": False,
        })
        self.store.save_profile(profile)
        self.profile = profile

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_manual_capture_persists_skill_mentions_and_explains_unknown_strength(self):
        description = "Maintain Windows Server and Active Directory."
        captured = capture_manual_job(self.store, {
            "request_id": "matching-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": description,
            "reference_url": "https://jobs.example.invalid/role/1",
            "application_url": "https://apply.example.invalid/role/1",
            "source_details": {"level": "unspecified"},
        })["job"]
        self.assertEqual(captured["description"], description)
        self.assertEqual(captured["skills"], ["Windows Server", "Active Directory"])
        self.assertEqual(captured["attributes"]["skill_evidence"], "description_mentions")

        recalculate(self.store, "fixture")
        match = self.store.job(captured["id"], profile_id="fixture")["match"]
        self.assertEqual(match["score"], 50.0)
        requirements = match["components"]["requirements"]
        self.assertEqual([row["strength"] for row in requirements], ["UNKNOWN", "UNKNOWN"])
        self.assertEqual(requirements[0]["profile_evidence"], "NO_APPROVED_EVIDENCE")
        self.assertEqual(requirements[1]["profile_evidence"], "APPROVED")
        self.assertIn("does not state", requirements[0]["explanation"].lower())
        self.assertEqual(match["missing"], ["Windows Server"])

    def test_manual_capture_persists_certification_strengths_for_reloaded_job(self):
        captured = capture_manual_job(self.store, {
            "request_id": "certification-strength-test", "title": "Systems Administrator",
            "company": "Example Infrastructure",
            "description": "CCNA. Security+ required. AZ-104 preferred. ITIL is a bonus.",
            "reference_url": "https://jobs.example.invalid/certifications/1",
        })["job"]

        self.store.engine.dispose()
        reopened = Store(self.store.url)
        try:
            requirements = reopened.job(captured["id"])["attributes"]["certifications"]
        finally:
            reopened.engine.dispose()

        self.assertEqual(
            {row["id"]: row["strength"] for row in requirements},
            {"CCNA": "UNKNOWN", "COMPTIA_SECURITY_PLUS": "REQUIRED",
             "AZ_104": "PREFERRED", "ITIL": "NICE_TO_HAVE"},
        )

    def test_manual_capture_preserves_negated_must_have_as_unknown_after_reload(self):
        captured = capture_manual_job(self.store, {
            "request_id": "negated-must-have-test", "title": "Systems Administrator",
            "company": "Example Infrastructure",
            "description": "CCNA is not a must-have. Security+ is a must-have.",
            "reference_url": "https://jobs.example.invalid/certifications/negated-must-have",
        })["job"]

        self.store.engine.dispose()
        reopened = Store(self.store.url)
        try:
            requirements = reopened.job(captured["id"])["attributes"]["certifications"]
        finally:
            reopened.engine.dispose()

        self.assertEqual(
            {row["id"]: row["strength"] for row in requirements},
            {"CCNA": "UNKNOWN", "COMPTIA_SECURITY_PLUS": "REQUIRED"},
        )

    def test_certification_fit_factors_use_approved_canonical_facts_and_preserve_strength(self):
        profile = copy.deepcopy(self.profile)
        profile["facts"].extend([
            {
                "id": "approved-ccna", "kind": "certification", "text": "CCNA",
                "source": "user-confirmed certification record", "confidence": 90,
                "verified": True, "user_approved": True,
            },
            {
                "id": "pending-security-plus", "kind": "certification",
                "text": "CompTIA Security+", "source": "imported record", "confidence": 100,
                "verified": False, "user_approved": False,
            },
            {
                "id": "skill-az104", "kind": "skill", "text": "AZ-104",
                "source": "user-confirmed skill fact", "confidence": 100,
                "verified": True, "user_approved": True,
            },
            {
                "id": "low-confidence-itil", "kind": "certification", "text": "ITIL",
                "source": "user-confirmed certification record", "confidence": 79,
                "verified": True, "user_approved": True,
            },
            {
                "id": "negative-mcsa", "kind": "certification", "text": "I don't hold MCSA",
                "source": "user-confirmed certification record", "confidence": 100,
                "verified": True, "user_approved": True,
            },
        ])
        self.store.save_profile(profile)
        captured = capture_manual_job(self.store, {
            "request_id": "certification-fit-factor-test", "title": "Systems Administrator",
            "company": "Example Infrastructure",
            "description": (
                "CCNA is required. Security+ preferred. AZ-104 is not required. ITIL. MCSA is not required."
            ),
            "reference_url": "https://jobs.example.invalid/certifications/match",
            "profile_id": "fixture",
        })["job"]

        recalculate(self.store, "fixture")
        result = self.store.job(captured["id"], profile_id="fixture")["match"]
        factors = {
            row["id"]: row
            for row in result["components"]["fit_factors"]
            if row["id"].startswith("certification:")
        }

        self.assertEqual(result["score"], 0)
        self.assertEqual(
            {row["id"]: row["strength"] for row in captured["attributes"]["certifications"]},
            {"CCNA": "REQUIRED", "COMPTIA_SECURITY_PLUS": "PREFERRED",
             "AZ_104": "UNKNOWN", "ITIL": "UNKNOWN", "MCSA": "UNKNOWN"},
        )
        self.assertEqual(factors["certification:CCNA"]["status"], "ALIGNS")
        self.assertIn("approved", factors["certification:CCNA"]["explanation"].lower())
        self.assertEqual(factors["certification:COMPTIA_SECURITY_PLUS"]["status"], "NO_DIRECT_MATCH")
        self.assertEqual(factors["certification:AZ_104"]["status"], "NO_DIRECT_MATCH")
        self.assertEqual(factors["certification:ITIL"]["status"], "NO_DIRECT_MATCH")
        self.assertEqual(factors["certification:MCSA"]["status"], "NO_DIRECT_MATCH")
        for certification_id, strength in (
            ("CCNA", "REQUIRED"),
            ("COMPTIA_SECURITY_PLUS", "PREFERRED"),
            ("AZ_104", "UNKNOWN"),
            ("ITIL", "UNKNOWN"),
            ("MCSA", "UNKNOWN"),
        ):
            factor = factors[f"certification:{certification_id}"]
            self.assertIn(strength, factor["label"])
            self.assertEqual(factor["score_effect"], "NONE")
        for certification_id in ("COMPTIA_SECURITY_PLUS", "AZ_104", "ITIL", "MCSA"):
            self.assertIn("no approved evidence", factors[f"certification:{certification_id}"]["explanation"].lower())
            self.assertIn("does not establish", factors[f"certification:{certification_id}"]["explanation"].lower())

    def test_certification_factor_is_durably_rescored_after_approval_is_revoked(self):
        profile = copy.deepcopy(self.profile)
        profile["facts"].append({
            "id": "rescore-ccna", "kind": "certification", "text": "Cisco Certified Network Associate (CCNA)",
            "source": "user-confirmed certification record", "confidence": 100,
            "verified": True, "user_approved": True,
        })
        self.store.save_profile(profile)
        captured = capture_manual_job(self.store, {
            "request_id": "certification-rescore-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": "CCNA preferred.",
            "reference_url": "https://jobs.example.invalid/certifications/rescore",
            "profile_id": "fixture",
        })["job"]

        recalculate(self.store, "fixture")
        before = self.store.job(captured["id"], profile_id="fixture")["match"]
        before_factor = next(
            row for row in before["components"]["fit_factors"]
            if row["id"] == "certification:CCNA"
        )
        self.assertEqual(before_factor["status"], "ALIGNS")
        self.assertEqual(before_factor["score_effect"], "NONE")

        profile["facts"] = [
            {**fact, "verified": False, "user_approved": False}
            if fact["id"] == "rescore-ccna" else fact
            for fact in profile["facts"]
        ]
        current_version = self.store.save_profile(profile)
        recalculate(self.store, "fixture")

        self.store.engine.dispose()
        reopened = Store(self.store.url)
        try:
            after = reopened.job(captured["id"], profile_id="fixture")["match"]
        finally:
            reopened.engine.dispose()

        after_factor = next(
            row for row in after["components"]["fit_factors"]
            if row["id"] == "certification:CCNA"
        )
        self.assertEqual(after["profile_version"], current_version)
        self.assertEqual(after["score"], before["score"])
        self.assertEqual(after_factor["status"], "NO_DIRECT_MATCH")
        self.assertIn("no approved evidence", after_factor["explanation"].lower())
        self.assertIn("does not establish", after_factor["explanation"].lower())
        self.assertEqual(after_factor["score_effect"], "NONE")

    def test_manual_skill_strengths_survive_reopen_and_weight_explainable_match(self):
        description = (
            "Windows Server is required. Active Directory is preferred. "
            "PowerShell is a nice to have. Linux."
        )
        captured = capture_manual_job(self.store, {
            "request_id": "skill-strength-reopen-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": description,
            "reference_url": "https://jobs.example.invalid/skill-strength/1",
            "profile_id": "fixture",
        })["job"]

        self.store.engine.dispose()
        reopened = Store(self.store.url)
        try:
            stored = reopened.job(captured["id"])
            self.assertEqual(stored["attributes"]["required_skills"], ["Windows Server"])
            self.assertEqual(stored["attributes"]["preferred_skills"], ["Active Directory"])
            self.assertEqual(stored["attributes"]["nice_to_have_skills"], ["PowerShell"])

            recalculate(reopened, "fixture")
            match = reopened.job(captured["id"], profile_id="fixture")["match"]
        finally:
            reopened.engine.dispose()

        self.assertEqual(match["score"], 25.0)
        rows = {row["skill"]: row for row in match["components"]["requirements"]}
        self.assertEqual(
            {skill: row["strength"] for skill, row in rows.items()},
            {"Windows Server": "REQUIRED", "Active Directory": "PREFERRED",
             "PowerShell": "NICE_TO_HAVE", "Linux": "UNKNOWN"},
        )
        self.assertLess(rows["Active Directory"]["weight"], rows["Windows Server"]["weight"])
        self.assertLess(rows["PowerShell"]["weight"], rows["Windows Server"]["weight"])
        self.assertEqual(rows["Windows Server"]["profile_evidence"], "NO_APPROVED_EVIDENCE")
        self.assertEqual(rows["Active Directory"]["profile_evidence"], "APPROVED")
        self.assertEqual(rows["PowerShell"]["profile_evidence"], "NO_APPROVED_EVIDENCE")
        self.assertEqual(rows["Linux"]["profile_evidence"], "NO_APPROVED_EVIDENCE")
        self.assertIn("does not state", rows["Linux"]["explanation"])
        self.assertIn("not qualification", match["components"]["limitation"])

    def test_explicit_preferred_requirement_has_lower_weight_than_required(self):
        from services.discovery.matching import build_explanation

        job = {
            "skills": ["Windows Server", "Active Directory"],
            "attributes": {
                "skill_evidence": "structured_requirements",
                "required_skills": ["Active Directory"],
                "preferred_skills": ["Windows Server"],
            },
        }
        profile = {"facts": [{
            "kind": "skill", "text": "Active Directory", "source": "approved",
            "confidence": 100, "verified": True, "user_approved": True,
        }]}
        result = build_explanation(job, profile)
        self.assertEqual(result["score"], 66.67)
        by_skill = {row["skill"]: row for row in result["components"]["requirements"]}
        self.assertEqual(by_skill["Active Directory"]["strength"], "REQUIRED")
        self.assertEqual(by_skill["Windows Server"]["strength"], "PREFERRED")
        self.assertLess(by_skill["Windows Server"]["weight"], by_skill["Active Directory"]["weight"])
        preferred_only = build_explanation(job, {"facts": [{
            "kind": "skill", "text": "Windows Server", "source": "approved",
            "confidence": 100, "verified": True, "user_approved": True,
        }]})
        self.assertEqual(preferred_only["score"], 33.33)

    def test_detected_skill_alias_matches_canonical_approved_fact(self):
        from services.discovery.matching import build_explanation

        result = build_explanation({
            "skills": ["AD"],
            "attributes": {"skill_evidence": "description_mentions"},
        }, {"facts": [{
            "kind": "skill", "text": "Active Directory", "source": "approved",
            "confidence": 100, "verified": True, "user_approved": True,
        }]})
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["components"]["matched"], ["Active Directory"])

    def test_title_and_work_arrangement_fit_are_explained_without_changing_skill_score(self):
        from services.discovery.matching import build_explanation

        result = build_explanation({
            "title": "Senior Windows Sysadmin",
            "remote_type": "UNKNOWN",
            "skills": ["Active Directory"],
            "attributes": {"skill_evidence": "description_mentions", "remote_indicated": True,
                           "employment_type": "FULL_TIME"},
        }, {
            "facts": [{
                "kind": "skill", "text": "Active Directory", "source": "approved",
                "confidence": 100, "verified": True, "user_approved": True,
            }],
            "preferences": {
                "desired_roles": ["Systems Administrator"],
                "work_arrangements": ["REMOTE"],
                "employment_types": ["FULL_TIME"],
            },
        })

        self.assertEqual(result["score"], 100)
        self.assertIn("fit_factors", result["components"])
        factors = {row["id"]: row for row in result["components"]["fit_factors"]}
        self.assertEqual(factors["title"]["status"], "ALIGNS")
        self.assertIn("Systems Administrator", factors["title"]["explanation"])
        self.assertEqual(factors["work_arrangement"]["status"], "ALIGNS")
        self.assertEqual(factors["employment_type"]["status"], "ALIGNS")

    def test_unavailable_fit_inputs_are_unknown_and_never_penalize_score(self):
        from services.discovery.matching import build_explanation

        result = build_explanation({
            "title": "Systems Administrator", "remote_type": "UNKNOWN",
            "skills": ["Active Directory"],
            "attributes": {"skill_evidence": "description_mentions"},
        }, {"facts": [], "preferences": {}})

        self.assertEqual(result["score"], 0)
        self.assertIn("fit_factors", result["components"])
        factors = {row["id"]: row for row in result["components"]["fit_factors"]}
        self.assertEqual(factors["title"]["status"], "UNKNOWN")
        self.assertEqual(factors["work_arrangement"]["status"], "UNKNOWN")
        self.assertTrue(all(row["score_effect"] == "NONE" for row in factors.values()))

    def test_queued_rescore_uses_latest_profile_version_after_approval_changes(self):
        job = capture_manual_job(self.store, {
            "request_id": "stale-rescore-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": "Active Directory",
            "reference_url": "https://jobs.example.invalid/role/2",
            "application_url": "https://apply.example.invalid/role/2",
        })["job"]
        old_version = self.store.profile("fixture")["version"]
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "profile_version": old_version, "rescore_only": True, "request_id": "stale-rescore-test",
        })
        changed = copy.deepcopy(self.profile)
        changed["facts"] = [{
            **fact,
            "verified": False if fact["id"] == "approved-ad" else fact["verified"],
            "user_approved": False if fact["id"] == "approved-ad" else fact["user_approved"],
        } for fact in changed["facts"]]
        current_version = self.store.save_profile(changed)
        self.assertNotEqual(old_version, current_version)

        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_OPERATION_MODE="FIXTURE", MJOBS_MOCK_ORIGIN="http://127.0.0.1:1",
                        MJOBS_OUTPUT_DIR=self.temp.name):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        match = self.store.job(job["id"], profile_id="fixture")["match"]
        self.assertEqual(match["profile_version"], current_version)
        self.assertEqual(match["score"], 0)

    def test_recalculate_reads_profile_after_entering_serialized_write(self):
        job = capture_manual_job(self.store, {
            "request_id": "profile-race-test", "title": "Systems Administrator",
            "company": "Example Infrastructure", "description": "Active Directory",
            "reference_url": "https://jobs.example.invalid/role/race",
            "application_url": "https://apply.example.invalid/role/race",
        })["job"]
        current = copy.deepcopy(self.profile)
        current["facts"] = [{
            **fact,
            "verified": False if fact["id"] == "approved-ad" else fact["verified"],
            "user_approved": False if fact["id"] == "approved-ad" else fact["user_approved"],
        } for fact in current["facts"]]
        original_begin = self.store.engine.begin
        changed_between_read_and_write = False

        def interleaved_begin(*args, **kwargs):
            nonlocal changed_between_read_and_write
            if not changed_between_read_and_write:
                changed_between_read_and_write = True
                self.store.save_profile(current)
            return original_begin(*args, **kwargs)

        with patch.object(self.store.engine, "begin", side_effect=interleaved_begin):
            recalculate(self.store, "fixture")

        match = self.store.job(job["id"], profile_id="fixture")["match"]
        self.assertEqual(match["profile_version"], self.store.profile("fixture")["version"])
        self.assertEqual(match["score"], 0)

    def test_generic_fixture_skills_do_not_become_required(self):
        from services.discovery.pipeline import mock_skill_evidence
        from services.discovery.matching import build_explanation

        skill_data, evidence = mock_skill_evidence({"skills": ["Python"]}, "Engineer", "Uses Azure")
        self.assertEqual(set(skill_data["labels"]), {"Python", "Azure"})
        self.assertEqual(evidence["skill_evidence"], "structured_requirements")
        self.assertEqual(evidence["required_skills"], ["Python"])
        explanation = build_explanation({
            "skills": skill_data["labels"], "attributes": evidence,
        }, {"facts": []})
        strengths = {row["skill"]: row["strength"] for row in explanation["components"]["requirements"]}
        self.assertEqual(strengths, {"Python": "REQUIRED", "Azure": "UNKNOWN"})

        skill_data, evidence = mock_skill_evidence({"required_skills": ["Python"]}, "Engineer", "Uses Azure")
        explanation = build_explanation({"skills": skill_data["labels"], "attributes": evidence}, {"facts": []})
        strengths = {row["skill"]: row["strength"] for row in explanation["components"]["requirements"]}
        self.assertEqual(strengths, {"Python": "REQUIRED", "Azure": "UNKNOWN"})


if __name__ == "__main__":
    unittest.main()
