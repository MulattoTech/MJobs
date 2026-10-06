import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.verification.reference_provenance import validate_reference_provenance


ROOT = Path(__file__).resolve().parents[2]


class ReferenceProvenanceTests(unittest.TestCase):
    def test_checked_in_registry_policy_template_and_notices_are_complete(self):
        self.assertEqual(validate_reference_provenance(ROOT), [])

    def test_registry_pins_license_evidence_to_each_inspected_source_commit(self):
        registry = json.loads((ROOT / "docs/reference/REFERENCE_PROJECTS.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry["projects"]), 7)
        for project in registry["projects"]:
            self.assertRegex(project["inspected_commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(project["license"]["source_url"].endswith(project["inspected_commit"] + "/" + project["license"]["path"]))
            self.assertRegex(project["license"]["git_blob_sha"], r"^[0-9a-f]{40}$")
            self.assertTrue(project["allowed_use_posture"].strip())

    def test_polyform_and_historical_relicense_are_explicit_and_fail_closed(self):
        registry = json.loads((ROOT / "docs/reference/REFERENCE_PROJECTS.json").read_text(encoding="utf-8"))
        projects = {row["id"]: row for row in registry["projects"]}
        autoapply = projects["AUTOAPPLY"]
        self.assertEqual(autoapply["license"]["spdx_id"], "NOASSERTION")
        self.assertEqual(autoapply["license"]["name"], "PolyForm Noncommercial License 1.0.0")
        self.assertIn("do not copy", autoapply["allowed_use_posture"].lower())
        lineage = projects["AIHAWK_INVISIBLE_PLAYWRIGHT"]["license_history"]
        self.assertEqual([row["spdx_id"] for row in lineage], ["AGPL-3.0", "MIT"])
        self.assertTrue(all("concept" in row["allowed_use_posture"].lower() for row in lineage))

    def test_validator_rejects_missing_commit_binding_and_evasion_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("docs/reference/REFERENCE_PROJECTS.json",
                             "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md",
                             "docs/reference/REFERENCE_INSPIRATION_LOG.md",
                             ".github/PULL_REQUEST_TEMPLATE.md", "THIRD_PARTY_NOTICES.md"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            registry_path = root / "docs/reference/REFERENCE_PROJECTS.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            del registry["projects"][0]["inspected_commit"]
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            policy_path = root / "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md"
            policy_path.write_text("Clean-room ideas only.", encoding="utf-8")

            errors = validate_reference_provenance(root)

        self.assertTrue(any("inspected_commit" in error for error in errors), errors)
        self.assertTrue(any("CAPTCHA" in error or "evasion" in error.lower() for error in errors), errors)

    def test_validator_rejects_license_url_for_a_different_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("docs/reference/REFERENCE_PROJECTS.json",
                             "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md",
                             "docs/reference/REFERENCE_INSPIRATION_LOG.md",
                             ".github/PULL_REQUEST_TEMPLATE.md", "THIRD_PARTY_NOTICES.md"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            registry_path = root / "docs/reference/REFERENCE_PROJECTS.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["projects"][0]["license"]["source_url"] = registry["projects"][1]["license"]["source_url"]
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            errors = validate_reference_provenance(root)

        self.assertTrue(any("repository must match" in error for error in errors), errors)

    def test_incorporated_source_requires_issue_project_and_commit_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("docs/reference/REFERENCE_PROJECTS.json",
                             "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md",
                             "docs/reference/REFERENCE_INSPIRATION_LOG.md",
                             ".github/PULL_REQUEST_TEMPLATE.md", "THIRD_PARTY_NOTICES.md"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            registry_path = root / "docs/reference/REFERENCE_PROJECTS.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["inspiration_records"][0]["third_party_code_or_assets_incorporated"] = True
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            errors = validate_reference_provenance(root)

        self.assertTrue(any("THIRD_PARTY_NOTICES.md" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
