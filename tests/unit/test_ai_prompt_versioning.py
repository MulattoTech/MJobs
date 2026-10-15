"""Unit tests for AI prompt and template versioning (MJ-REQ-270)."""

import unittest
from services.ai.prompt_versioning import (
    PROMPT_TARGETS,
    PromptRegistry,
    PromptTarget,
    PromptVersion,
)


class AIPromptVersioningTests(unittest.TestCase):
    def test_all_four_authoritative_targets_supported_with_defaults(self):
        reg = PromptRegistry()
        self.assertEqual(
            set(PROMPT_TARGETS),
            {"resume", "cover_letter", "screening_answers", "job_parsing"},
        )
        for target in PROMPT_TARGETS:
            active = reg.get_active_version(target)
            self.assertEqual(active.version, "1.0.0")
            self.assertEqual(active.target, target)
            self.assertTrue(active.sha256)
            self.assertTrue(len(active.variables) > 0)

    def test_render_prompt_traces_exact_version_used(self):
        reg = PromptRegistry()
        rendered, version = reg.render(
            PromptTarget.RESUME,
            job_title="DevOps Engineer",
            company_name="CloudCo",
            career_facts="10 years Linux experience",
        )
        self.assertEqual(version, "1.0.0")
        self.assertIn("DevOps Engineer", rendered)
        self.assertIn("CloudCo", rendered)
        self.assertIn("10 years Linux experience", rendered)

    def test_render_missing_variable_fails_closed(self):
        reg = PromptRegistry()
        with self.assertRaises(ValueError) as error:
            reg.render(
                PromptTarget.RESUME,
                job_title="DevOps Engineer",
                # missing company_name and career_facts
            )
        self.assertIn("PROMPT_VARIABLE_MISSING", str(error.exception))

    def test_register_new_version_updates_active_and_hashes_cryptographically(self):
        reg = PromptRegistry()
        new_template = "Tailor v2:\nRole: {job_title}\nCompany: {company_name}\nFacts: {career_facts}"
        v2 = reg.register_version(
            PromptTarget.RESUME,
            version="2.0.0",
            template=new_template,
            description="Upgraded concise prompt",
            set_active=True,
        )

        self.assertEqual(v2.version, "2.0.0")
        self.assertEqual(reg.get_active_version(PromptTarget.RESUME).version, "2.0.0")

        # Can still access and render historical v1.0.0
        v1 = reg.get_version(PromptTarget.RESUME, "1.0.0")
        self.assertEqual(v1.version, "1.0.0")

        rendered, ver = reg.render(
            PromptTarget.RESUME,
            version="1.0.0",
            job_title="Engineer",
            company_name="Acme",
            career_facts="Skills",
        )
        self.assertEqual(ver, "1.0.0")

    def test_duplicate_version_registration_is_rejected(self):
        reg = PromptRegistry()
        with self.assertRaises(ValueError) as error:
            reg.register_version(
                PromptTarget.RESUME,
                version="1.0.0",
                template="Another template",
            )
        self.assertIn("PROMPT_VERSION_ALREADY_EXISTS", str(error.exception))

    def test_invalid_target_and_version_format_fail_closed(self):
        reg = PromptRegistry()
        with self.assertRaises(ValueError) as target_err:
            reg.register_version(
                "invalid_target",
                version="1.0.0",
                template="template",
            )
        self.assertIn("PROMPT_TARGET_INVALID", str(target_err.exception))

        with self.assertRaises(ValueError) as ver_err:
            reg.register_version(
                PromptTarget.RESUME,
                version="invalid-version-string",
                template="template",
            )
        self.assertIn("PROMPT_VERSION_INVALID", str(ver_err.exception))

    def test_registry_serialization_and_roundtrip(self):
        reg = PromptRegistry()
        reg.register_version(
            PromptTarget.COVER_LETTER,
            version="1.1.0",
            template="Cover letter {job_title} at {company_name}: {candidate_profile} {qualifications}",
            description="Minor refinement",
            set_active=True,
        )
        serialized = reg.to_dict()

        reconstructed = PromptRegistry.from_dict(serialized)
        self.assertEqual(
            reconstructed.get_active_version(PromptTarget.COVER_LETTER).version,
            "1.1.0",
        )
        self.assertEqual(
            reconstructed.get_version(PromptTarget.COVER_LETTER, "1.0.0").version,
            "1.0.0",
        )


class PersistedPromptProvenanceTests(unittest.TestCase):
    def setUp(self):
        from services.ai.prompt_provenance import capture_document
        self.snapshot = capture_document("screening_answers", "reviewed-application-answers-v1",
                                         "manual-reviewed-answers-v1")

    def reseal(self, mutation):
        """Corrupt shape even with a matching hash, to test schema independently."""
        from copy import deepcopy
        import hashlib
        import json
        snapshot = deepcopy(self.snapshot)
        manifest = json.loads(snapshot["definition"]["template"])
        mutation(manifest)
        text = json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(text.encode()).hexdigest()
        snapshot["definition"].update(template=text, sha256=digest,
                                          version="1.0.0-sha256." + digest)
        return snapshot

    def test_stored_manifest_never_resolves_imports_paths_or_current_source(self):
        from unittest.mock import patch
        from services.ai.prompt_provenance import resolve_provenance
        with patch("services.ai.prompt_provenance._helpers", side_effect=AssertionError("no source reads")), \
                patch("pathlib.Path.read_bytes", side_effect=AssertionError("no file reads")), \
                patch("importlib.import_module", side_effect=AssertionError("no dynamic imports")):
            resolved = resolve_provenance(self.snapshot)
        self.assertEqual(resolved["target"], "screening_answers")
        self.assertFalse(resolved["ai_executed"])

    def test_tampered_helper_paths_producers_shapes_and_oversize_are_rejected(self):
        from services.ai.prompt_provenance import resolve_provenance
        mutations = (
            lambda value: value["helpers"][0].update(module="../../private"),
            lambda value: value["helpers"][0].update(module="services.documents.unknown"),
            lambda value: value["helpers"][0].update(path="C:/private"),
            lambda value: value["helpers"].append(value["helpers"][0]),
            lambda value: value["helpers"].clear(),
            lambda value: value["helpers"][0].update(sha256="not-a-digest"),
            lambda value: value["definitions"][0].update(entrypoint="os.system"),
            lambda value: value["definitions"][0]["constants"].update(rendered_input="private"),
            lambda value: value["definitions"][0].update(source="x" * 131072),
            lambda value: value.update(unknown="shape"),
            lambda value: value.update(target="resume"),
        )
        for number, mutation in enumerate(mutations):
            with self.subTest(mutation=number), self.assertRaises(ValueError):
                resolve_provenance(self.reseal(mutation))

    def test_unknown_parser_is_explicitly_unsupported_without_fabricated_version(self):
        from services.ai.prompt_provenance import capture_parser, resolve_provenance
        result = capture_parser(object())
        self.assertEqual(result["status"], "UNSUPPORTED_PRODUCER")
        self.assertNotIn("definition", result)
        with self.assertRaises(ValueError):
            resolve_provenance(result)

    def test_nested_static_definition_shape_is_checked(self):
        from services.ai.prompt_provenance import capture_document, resolve_provenance
        from services.documents.templates import BASE_TEMPLATE, BASE_MODEL
        self.snapshot = capture_document("resume", BASE_TEMPLATE, BASE_MODEL)
        corrupted = self.reseal(lambda value: value["definitions"][0]["constants"].update(HEADINGS=[]))
        with self.assertRaises(ValueError):
            resolve_provenance(corrupted)


if __name__ == "__main__":
    unittest.main()
