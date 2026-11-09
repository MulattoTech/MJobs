"""Saved-resume audit contracts: recorded targets, literal text and safe absence."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from services.documents.quality_audit import audit_saved_resume


def saved_document(terms=None):
    return {
        "id": "resume-document-1",
        "kind": "resume",
        "version": 2,
        "profile_version": hashlib.sha256(b"saved-profile-version").hexdigest(),
        "job_id": "recorded-job-1",
        "sha256": {"txt": hashlib.sha256(b"saved-artifact-bytes\n").hexdigest()},
        "generation_context": {
            "mode": "APPROVED_SKILL_ORDER",
            "job_id": "recorded-job-1",
            "job_skills": ["Python", "SQL"] if terms is None else terms,
            "target_fingerprint": hashlib.sha256(b"recorded-target").hexdigest(),
        },
    }


class DocumentQualityAuditTests(unittest.TestCase):
    def audit(self, document=None, text="Python.", **kwargs):
        return audit_saved_resume(
            saved_document() if document is None else document,
            text,
            validation_state=kwargs.get("validation_state", "VALIDATED"),
        )

    def assert_unavailable(self, result, reason):
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["reason"], reason)
        self.assertIsNone(result["source"])
        self.assertEqual(result["present_terms"], [])
        self.assertEqual(result["missing_terms"], [])
        self.assertEqual(result["term_count"], 0)

    def test_audits_saved_text_instead_of_source_facts_or_recorded_match_labels(self):
        document = saved_document()
        document["facts_used"] = [{"text": "Python and SQL", "kind": "skill"}]
        document["generation_context"]["matched_skills"] = {"source-fact": ["Python", "SQL"]}
        result = self.audit(document, "Built Python tools; used PostgreSQL.")
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["reason"], "RECORDED_TARGET_TERMS")
        self.assertEqual(result["present_terms"], ["Python"])
        self.assertEqual(result["missing_terms"], ["SQL"])
        self.assertEqual(result["term_count"], 2)
        self.assertEqual(result["factual_validation"], "VALIDATED_SAVED_SOURCES")

    def test_matches_literal_terms_without_aliases_stemming_or_scores(self):
        terms = ["PostgreSQL", "Amazon Web Services", "K8s", "C sharp", "developer"]
        result = self.audit(saved_document(terms), "SQL, AWS, Kubernetes, C#, development.")
        self.assertEqual(result["present_terms"], [])
        self.assertEqual(result["missing_terms"], terms)
        self.assertEqual(set(result), {
            "schema_version", "method", "status", "reason", "factual_validation",
            "source", "present_terms", "missing_terms", "term_count",
        })
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["method"], "CASEFOLD_LITERAL_TERMS_V1")

    def test_whole_terms_exclude_substrings_and_other_languages(self):
        result = self.audit(saved_document(["Python", "SQL", "C"]),
                            "CPython Python3 Python.py foo.Python PostgreSQL SQLAlchemy C++ C#")
        self.assertEqual(result["present_terms"], [])
        self.assertEqual(result["missing_terms"], ["Python", "SQL", "C"])

    def test_punctuation_skills_match_and_sentence_punctuation_is_not_part_of_term(self):
        result = self.audit(saved_document(["C", "C++", "C#", ".NET", "Node.js"]),
                            "Used C++, C#, .NET, and Node.js.")
        self.assertEqual(result["present_terms"], ["C++", "C#", ".NET", "Node.js"])
        self.assertEqual(result["missing_terms"], ["C"])
        self.assertEqual(self.audit(saved_document(["C", "Python"]), "(C). Python.")["present_terms"],
                         ["C", "Python"])
        self.assertEqual(self.audit(saved_document([".NET"]), "ASP.NET and .NETCore")["present_terms"], [])

    def test_hyphenated_phrases_and_slash_separated_lists_contain_literal_terms(self):
        terms = ["Python", "C", "C++", "SQL"]
        result = self.audit(saved_document(terms),
                            "Python-based automation; C/C++ development and SQL-backed services.")
        self.assertEqual(result["present_terms"], terms)
        self.assertEqual(result["missing_terms"], [])

    def test_regular_expression_characters_are_escaped_literal_targets(self):
        terms = ["C.*", "C[ab]", "a|b"]
        self.assertEqual(self.audit(saved_document(terms), "Canything Ca Cb a b")["present_terms"], [])
        self.assertEqual(self.audit(saved_document(terms), "C.*; C[ab]; a|b.")["present_terms"], terms)

    def test_valid_overlapping_multiword_match_is_not_skipped_after_invalid_prefix(self):
        result = self.audit(saved_document(["a a"]), "xa a a")
        self.assertEqual(result["present_terms"], ["a a"])

    def test_unicode_casefold_and_whitespace_deduplicate_preserving_first_labels(self):
        terms = [" SQL\t Server ", "sql server", "PYTHON", "Python", "Straße", "STRASSE", "σ"]
        document = saved_document(terms)
        result = self.audit(document, "python\nSQL\u00a0Server; STRASSE, ς.")
        self.assertEqual(result["present_terms"], ["SQL Server", "PYTHON", "Straße", "σ"])
        self.assertEqual(result["missing_terms"], [])
        self.assertEqual(result["term_count"], 4)
        raw_snapshot_hash = hashlib.sha256(json.dumps(
            terms, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.assertEqual(result["source"]["target_terms_sha256"], raw_snapshot_hash)

    def test_unicode_combining_marks_do_not_create_false_whole_terms(self):
        result = self.audit(saved_document(["i", "Cafe"]), "İ Cafe\u0301")
        self.assertEqual(result["present_terms"], [])
        self.assertEqual(self.audit(saved_document(["İ"]), "İ")["present_terms"], ["İ"])
        # Casefold is not compatibility normalization or accent stripping.
        self.assertEqual(self.audit(saved_document(["Python"]), "Ｐｙｔｈｏｎ")["present_terms"], [])

    def test_output_contains_only_bound_source_identity_and_target_lists(self):
        document = saved_document()
        document["generation_context"]["contact_header"] = ["private.fixture@example.test"]
        document["generation_context"]["unrelated"] = {"arbitrary": "DO_NOT_RETURN_CONTEXT"}
        result = self.audit(document, "Private Fixture\nprivate.fixture@example.test\nPython.")
        self.assertEqual(result["source"], {
            "document_id": document["id"],
            "document_version": document["version"],
            "profile_version": document["profile_version"],
            "text_sha256": document["sha256"]["txt"],
            "target_fingerprint": document["generation_context"]["target_fingerprint"],
            "target_terms_sha256": hashlib.sha256(b'["Python","SQL"]').hexdigest(),
            "job_id": document["job_id"],
        })
        encoded = json.dumps(result)
        for private_value in ("Private Fixture", "private.fixture@example.test", "DO_NOT_RETURN_CONTEXT"):
            self.assertNotIn(private_value, encoded)

    def test_recorded_text_digest_is_not_replaced_by_hash_of_normalized_preview(self):
        document = saved_document()
        first = self.audit(document, "Python\nSQL")
        second = self.audit(document, "PYTHON SQL")
        self.assertEqual(first["source"]["text_sha256"], document["sha256"]["txt"])
        self.assertEqual(first["source"], second["source"])
        self.assertNotEqual(first["source"]["text_sha256"], hashlib.sha256(b"python sql").hexdigest())

    def test_snapshot_hash_binds_original_order_spelling_and_duplicate_entries(self):
        first = self.audit(saved_document(["Python", "PYTHON", "SQL"]))
        second = self.audit(saved_document(["Python", "SQL"]))
        self.assertEqual(first["present_terms"], second["present_terms"])
        self.assertEqual(first["missing_terms"], second["missing_terms"])
        self.assertNotEqual(first["source"]["target_terms_sha256"], second["source"]["target_terms_sha256"])

    def test_repeatable_pure_read_does_not_mutate_input_or_alias_output_lists(self):
        document = saved_document([" Python ", "python", "SQL"])
        original = deepcopy(document)
        with patch("builtins.open", side_effect=AssertionError("Audit must not open files")):
            first = self.audit(document)
            second = self.audit(document)
        self.assertEqual(first, second)
        self.assertEqual(document, original)
        first["present_terms"].append("MUTATED_RESULT")
        first["source"]["document_id"] = "changed-result-only"
        self.assertEqual(document, original)
        self.assertNotIn("MUTATED_RESULT", second["present_terms"])

    def test_unvalidated_missing_or_non_text_preview_never_reports_coverage(self):
        for state in (None, "NOT_RUN", "FAILED", "validated", True, 1, [], {}):
            with self.subTest(state=state):
                result = self.audit(validation_state=state)
                self.assert_unavailable(result, "DOCUMENT_NOT_VALIDATED")
                self.assertEqual(result["factual_validation"], "NOT_VALIDATED")
        for text in (None, b"Python", 1, True, ["Python"], {"text": "Python"}, "Python\x00SQL", "Python\ud800"):
            with self.subTest(text=repr(text)):
                result = self.audit(text=text)
                self.assert_unavailable(result, "DOCUMENT_NOT_VALIDATED")
                self.assertEqual(result["factual_validation"], "NOT_VALIDATED")

    def test_empty_or_whitespace_only_saved_text_is_not_a_validated_preview(self):
        for text in ("", " ", "\n\t\u00a0"):
            with self.subTest(text=repr(text)):
                result = self.audit(text=text)
                self.assert_unavailable(result, "DOCUMENT_NOT_VALIDATED")
                self.assertEqual(result["factual_validation"], "NOT_VALIDATED")

    def test_invalid_identity_is_unavailable_and_never_echoes_untrusted_values(self):
        replacements = {
            "id": [None, "", "x" * 129, "../private", "private@example.test", 1],
            "job_id": [None, "", "x" * 129, "job\nprivate", False],
            "version": [None, 0, -1, True, "2", 2.5, 2**53],
            "profile_version": [None, "", "x" * 129, 2, "private@example.test"],
            "kind": [None, "cover-letter", "application-answer-export", 1],
            "sha256": [None, [], {"txt": "f" * 63}, {"txt": "g" * 64}, {"txt": 12}],
        }
        for key, values in replacements.items():
            for value in values:
                with self.subTest(field=key, value=repr(value)):
                    document = saved_document()
                    document[key] = value
                    self.assert_unavailable(self.audit(document), "DOCUMENT_IDENTITY_INVALID")
        for value in (None, [], "document", 1, True):
            with self.subTest(document=value):
                self.assert_unavailable(audit_saved_resume(value, "Python", validation_state="VALIDATED"),
                                        "DOCUMENT_IDENTITY_INVALID")

    def test_valid_boundary_identifiers_and_uppercase_digests_preserve_recorded_identity(self):
        document = saved_document()
        document["id"] = "d" * 128
        document["job_id"] = "j" * 128
        document["generation_context"]["job_id"] = document["job_id"]
        document["version"] = 2**53 - 1
        document["sha256"]["txt"] = document["sha256"]["txt"].upper()
        document["generation_context"]["target_fingerprint"] = document["generation_context"]["target_fingerprint"].upper()
        result = self.audit(document)
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["source"]["text_sha256"], document["sha256"]["txt"])
        self.assertEqual(result["source"]["document_version"], 2**53 - 1)

    def test_missing_or_untailored_context_is_unavailable(self):
        for context in (None, {}, {"mode": "APPROVED_FACTS"}, {"mode": "APPROVED_CONTACT_HEADER"}):
            with self.subTest(context=context):
                document = saved_document()
                document["generation_context"] = context
                self.assert_unavailable(self.audit(document), "TARGET_SNAPSHOT_MISSING")

    def test_known_invalid_responsibility_provenance_blocks_an_otherwise_valid_audit(self):
        document = saved_document()
        document["responsibility_context_valid"] = False
        result = self.audit(document)
        self.assert_unavailable(result, "TARGET_SNAPSHOT_INVALID")
        self.assertEqual(result["factual_validation"], "VALIDATED_SAVED_SOURCES")
        document["responsibility_context_valid"] = True
        self.assertEqual(self.audit(document)["status"], "AVAILABLE")
        del document["responsibility_context_valid"]
        self.assertEqual(self.audit(document)["status"], "AVAILABLE")

    def test_malformed_target_snapshot_does_not_silently_drop_invalid_entries(self):
        replacements = {
            "mode": [[], True],
            "job_id": [None, "different-job", True, "x" * 129],
            "target_fingerprint": [None, "", "x" * 64, "f" * 63, 1],
            "job_skills": [None, "Python", ("Python",), {}, ["Python", 1], ["Python", " "],
                           ["Python", "SQL\x00"], ["Python", "SQL\u202e"], ["Python", "\ud800"]],
        }
        for key, values in replacements.items():
            for value in values:
                with self.subTest(field=key, value=repr(value)):
                    document = saved_document()
                    document["generation_context"][key] = value
                    self.assert_unavailable(self.audit(document), "TARGET_SNAPSHOT_INVALID")
        document = saved_document()
        document["generation_context"] = ["APPROVED_SKILL_ORDER"]
        self.assert_unavailable(self.audit(document), "TARGET_SNAPSHOT_INVALID")

    def test_empty_target_list_is_unavailable_not_an_empty_denominator(self):
        result = self.audit(saved_document([]))
        self.assert_unavailable(result, "NO_TARGET_TERMS")
        self.assertEqual(result["factual_validation"], "VALIDATED_SAVED_SOURCES")

    def test_target_count_bound_applies_before_deduplication(self):
        accepted = self.audit(saved_document(["Python"] * 100))
        self.assertEqual(accepted["status"], "AVAILABLE")
        self.assertEqual(accepted["term_count"], 1)
        self.assert_unavailable(self.audit(saved_document(["Python"] * 101)), "AUDIT_INPUT_LIMIT_EXCEEDED")

    def test_term_limits_fail_closed_on_raw_or_casefold_expanded_text(self):
        term = "x" * 120
        self.assertEqual(self.audit(saved_document([term]), term)["present_terms"], [term])
        for term in ("x" * 121, " " * 121 + "Python", "ß" * 61):
            with self.subTest(term_length=len(term)):
                self.assert_unavailable(self.audit(saved_document([term])), "AUDIT_INPUT_LIMIT_EXCEEDED")

    def test_preview_limit_applies_before_whitespace_reduction_and_after_casefold(self):
        self.assertEqual(self.audit(text="Python" + " " * 199_994)["present_terms"], ["Python"])
        for text in ("Python" + " " * 199_995, "ß" * 100_001):
            with self.subTest(length=len(text)):
                self.assert_unavailable(self.audit(text=text), "AUDIT_INPUT_LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
