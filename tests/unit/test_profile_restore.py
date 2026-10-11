import base64
import copy
import hashlib
import json
import unittest

from services.profile_restore import (
    ProfileRestoreError,
    build_restore_plan,
    preview_profile_restore,
)


def _identity(*parts):
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_record(identifier, file_format, content):
    return {
        "id": identifier,
        "format": file_format,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _payload():
    profile_id = "source-profile"
    current_version = "v-current"
    contents = b"synthetic uploaded resume"
    generated = b"synthetic generated text"
    profile = {
        "id": profile_id,
        "name": "Synthetic Person",
        "email": "person@example.test",
        "version": current_version,
    }
    current_snapshot = {
        "id": _identity(profile_id, current_version),
        "profile_id": profile_id,
        "version": current_version,
        "contact": {
            "name": profile["name"],
            "email": profile["email"],
            "contact": {"first_name": "Synthetic", "last_name": "Person"},
            "preferences": {
                "company_discovery": {"company_discovery_enabled": True},
                "application_route_preference": "PREFER_VERIFIED",
                "sensitive_answer_policies": {
                    "RACE_ETHNICITY": {
                        "mode": "USE_CONFIGURED_RESPONSE", "response": "PRIVATE ANSWER",
                    },
                },
            },
        },
        "answers": [{
            "id": "answer-1", "question": "Work authorization?", "text": "Yes",
            "source": "user", "verified": True, "user_approved": True, "confidence": 100,
        }],
    }
    return {
        "schema_version": 1,
        "export_type": "profile_backup",
        "profile_id": profile_id,
        "profile": profile,
        "profile_versions": [current_snapshot],
        "facts": [{
            "id": "fact-row-1", "profile_id": profile_id, "version": current_version,
            "fact_id": "fact-1", "kind": "skill", "text": "Python",
            "source": "resume extraction", "verified": True, "user_approved": True,
            "confidence": 95,
        }],
        "uploaded_resumes": [{
            "id": "upload-1", "filename": "resume.pdf", "format": "pdf",
            "media_type": "application/pdf", "size_bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(), "status": "READY",
            "created_at": "2026-10-11T12:00:00Z",
        }],
        "resume_revisions": [{
            "id": "resume-revision-1", "profile_id": profile_id, "upload_id": "upload-1",
            "series_id": "series-1", "label": "Base", "version": 1,
            "document_kind": "BASE_RESUME",
        }],
        "document_files": [
            _file_record("upload-1", "pdf", contents),
            _file_record("generated-doc-1", "txt", generated),
        ],
        "documents": [{
            "id": "generated-doc-1", "application_id": "application-1", "kind": "resume",
            "sha256": {"txt": hashlib.sha256(generated).hexdigest()},
        }],
        "settings": {"search_profiles": [{
            "id": "search-1", "name": "Remote roles", "enabled": True,
            "criteria": {"remote_only": True, "scheduler": {"enabled": True}},
            "source_ids": ["source-1"],
        }]},
        "jobs": [{"id": "private-job", "title": "Omitted job"}],
        "applications": [{"id": "private-application", "state": "SUBMITTED"}],
        "provider_credentials": [{"secret": "must never be copied"}],
    }


class ProfileRestoreTests(unittest.TestCase):
    def test_preview_is_value_free_bounded_and_reports_fixed_sections(self):
        payload = _payload()

        result = preview_profile_restore(payload)
        json_result = preview_profile_restore(json.dumps(payload))

        self.assertEqual(result["source_profile"], {
            "name": "Synthetic Person", "email": "person@example.test",
        })
        self.assertEqual(result["counts"], {
            "facts": 1, "answers": 1, "uploaded_files": 1, "search_profiles": 1,
        })
        self.assertIn("IMPORTED_FACTS_REQUIRE_REVIEW", result["warnings"])
        self.assertIn("SEARCH_PROFILES_DISABLED", result["warnings"])
        self.assertIn("job_history", result["omitted_sections"])
        self.assertEqual(len(result["fingerprint"]), 64)
        self.assertEqual(json_result["fingerprint"], result["fingerprint"])
        rendered = json.dumps(result, sort_keys=True)
        for secret in ("Python", "PRIVATE ANSWER", "private-job", "must never be copied"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result["fingerprint"], preview_profile_restore(payload)["fingerprint"])

    def test_restore_plan_normalizes_profile_resets_verification_and_disables_automation(self):
        plan = build_restore_plan(_payload(), "restored-profile")

        profile = plan["profile"]
        self.assertEqual(profile["id"], "restored-profile")
        self.assertEqual(profile["name"], "Synthetic Person")
        self.assertEqual(profile["facts"][0]["id"], "fact-1")
        self.assertFalse(profile["facts"][0]["verified"])
        self.assertFalse(profile["facts"][0]["user_approved"])
        self.assertEqual(profile["facts"][0]["confidence"], 0.0)
        self.assertFalse(profile["answers"][0]["verified"])
        self.assertFalse(profile["answers"][0]["user_approved"])
        self.assertEqual(profile["answers"][0]["confidence"], 0.0)
        self.assertFalse(profile["preferences"]["company_discovery"]["company_discovery_enabled"])
        self.assertEqual(profile["preferences"]["application_route_preference"], "USER_CHOICE")
        self.assertEqual(profile["preferences"]["sensitive_answer_policies"]["RACE_ETHNICITY"], {
            "mode": "ALWAYS_REQUIRE_REVIEW", "response": None,
        })
        self.assertEqual(plan["uploaded_files"][0]["content"], b"synthetic uploaded resume")
        self.assertNotIn("private-job", json.dumps({k: v for k, v in plan.items()
                                                     if k != "uploaded_files"}, default=str))
        search = plan["search_profiles"][0]
        self.assertFalse(search["enabled"])
        self.assertEqual(search["source_ids"], [])
        self.assertNotIn("scheduler", search["criteria"])

    def test_missing_optional_file_bytes_are_explicitly_warned(self):
        payload = _payload()
        payload["document_files"] = []

        result = preview_profile_restore(payload)
        plan = build_restore_plan(payload, "restored-profile")

        self.assertIn("UPLOADED_FILES_NOT_INCLUDED", result["warnings"])
        self.assertEqual(result["counts"]["uploaded_files"], 0)
        self.assertEqual(plan["uploaded_files"], [])

    def test_rejects_unsupported_version_export_and_malformed_current_snapshot(self):
        for update in (
            {"schema_version": 2},
            {"export_type": "database_dump"},
        ):
            payload = _payload()
            payload.update(update)
            with self.subTest(update=update), self.assertRaises(ProfileRestoreError):
                preview_profile_restore(payload)

        payload = _payload()
        payload["profile_versions"][0]["profile_id"] = "foreign-profile"
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "restored-profile")

    def test_rejects_duplicate_fact_and_answer_ids_and_bad_file_integrity(self):
        payload = _payload()
        payload["facts"].append(copy.deepcopy(payload["facts"][0]))
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "restored-profile")

        payload = _payload()
        payload["profile_versions"][0]["answers"].append(
            copy.deepcopy(payload["profile_versions"][0]["answers"][0]))
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "restored-profile")

        payload = _payload()
        original_size = payload["document_files"][0]["size_bytes"]
        payload["document_files"][0]["content_base64"] = base64.b64encode(
            b"X" * original_size,
        ).decode()
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "restored-profile")

    def test_rejects_source_profile_target_and_cross_profile_resume_revision(self):
        payload = _payload()
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "source-profile")

        payload["resume_revisions"][0]["profile_id"] = "foreign-profile"
        with self.assertRaises(ProfileRestoreError):
            build_restore_plan(payload, "restored-profile")

    def test_rejects_oversize_deep_and_duplicate_key_json(self):
        payload = _payload()
        with self.assertRaises(ProfileRestoreError):
            preview_profile_restore(json.dumps(payload) + " " * (5 * 1024 * 1024))

        nested = _payload()
        value = []
        for _ in range(50):
            value = [value]
        nested["extra"] = value
        with self.assertRaises(ProfileRestoreError):
            preview_profile_restore(nested)

        with self.assertRaises(ProfileRestoreError):
            preview_profile_restore('{"schema_version":1,"schema_version":1}')


if __name__ == "__main__":
    unittest.main()
