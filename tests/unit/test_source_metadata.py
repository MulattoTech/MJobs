"""Public metadata fidelity, explicit omissions, and bounded read projection."""
from copy import deepcopy
import json
import unittest

from services.normalization.jobs import canonical_job_view, raw_posting_view
from services.normalization import source_metadata as metadata


def posting(raw, **overrides):
    return {
        "id": "posting-one", "job_id": "job-one", "source": "public-board",
        "source_job_id": "native-123", "source_url": "https://jobs.example.test/feed",
        "application_url": "https://jobs.example.test/apply/123",
        "fetched_at": "2026-11-09T03:00:00+00:00", "raw": raw, **overrides,
    }


class SourceMetadataTests(unittest.TestCase):
    def test_unknown_nested_json_is_detached_and_round_trippable(self):
        raw = {
            "novel_vendor_field": {"rating": 0, "enabled": False, "optional": None,
                                   "empty": {}, "items": [], "label": "Développeur 東京"},
            "new_list": [None, 0, False, "", {"unknown": [1.5, True]}],
        }
        row = posting(raw)
        before = deepcopy(row)
        envelope = metadata.source_metadata_view(row)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["status"], "COMPLETE")
        self.assertEqual(json.loads(json.dumps(envelope["extensions"])), raw)
        self.assertEqual(envelope["source"]["posting_id"], "posting-one")
        self.assertEqual(envelope["source"]["source_job_id"], "native-123")
        self.assertEqual(envelope["source"]["source_identifier"], "public-board")
        envelope["extensions"]["novel_vendor_field"]["rating"] = 9
        self.assertEqual(row, before)

    def test_raw_read_view_preserves_original_payload_and_urls(self):
        row = posting({"new_public_field": {"enabled": False}})
        result = raw_posting_view(row)
        self.assertIs(result["raw"], row["raw"])
        self.assertIs(result["raw_structured_payload"], row["raw"])
        self.assertEqual(result["source_url"], row["source_url"])
        self.assertEqual(result["source_metadata"]["extensions"], row["raw"])

    def test_metadata_cannot_change_canonical_fields(self):
        canonical = {"id": "job-one", "title": "Stored title", "description": "Stored text",
                     "skills": [], "attributes": {"provider": "public-board", "salary_min": 0}}
        before = deepcopy(canonical)
        result = canonical_job_view(canonical)
        self.assertEqual(result["title"], "Stored title")
        self.assertEqual(result["salary_min"], 0)
        self.assertEqual(canonical, before)

        claims = {"schema_version": 999, "source": "asserted-origin", "id": "asserted-id",
                  "title": "Asserted title", "authority": "VERIFIED", "confidence": 1}
        envelope = metadata.source_metadata_view(posting(claims))
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["source"]["posting_id"], "posting-one")
        self.assertEqual(envelope["source"]["source_identifier"], "public-board")
        self.assertEqual(envelope["extensions"], claims)

    def test_private_nested_keys_and_unsafe_urls_have_explicit_omissions(self):
        secret = "PRIVATE_METADATA_CANARY"
        raw = {
            "public": {"rating": 4, "api_key": secret, "browserSession": {"state": secret}},
            "headers": {"Authorization": secret},
            "company_url": "https://example.test/company",
            "signed_url": f"https://example.test/feed?credential={secret}",
            "links": ["https://example.test/public", f"https://person:{secret}@example.test"],
        }
        envelope = metadata.source_metadata_view(posting(raw))
        self.assertEqual(envelope["status"], "PARTIAL")
        self.assertEqual(envelope["omissions"], {"private_fields": 3, "unsafe_urls": 2,
                                                "source_identity_fields": 0})
        self.assertEqual(envelope["reason_codes"], ["PRIVATE_FIELDS_OMITTED", "UNSAFE_URLS_OMITTED"])
        self.assertEqual(envelope["extensions"]["public"], {"rating": 4})
        self.assertEqual(envelope["extensions"]["links"], ["https://example.test/public", None])
        self.assertNotIn(secret, json.dumps(envelope))
        self.assertIn(secret, json.dumps(raw))

    def test_unsafe_source_endpoint_is_omitted_without_changing_stored_source(self):
        source = "https://example.test/feed?token=PRIVATE_ENDPOINT_CANARY"
        row = posting({"industry": "technology"}, source=source, source_url=source)
        envelope = metadata.source_metadata_view(row)
        self.assertIsNone(envelope["source"]["source_url"])
        self.assertIsNone(envelope["source"]["source_identifier"])
        self.assertEqual(envelope["status"], "PARTIAL")
        self.assertNotIn("PRIVATE_ENDPOINT_CANARY", json.dumps(envelope))
        self.assertEqual(row["source_url"], source)

    def test_unsafe_or_invalid_source_identity_is_omitted_explicitly(self):
        marker = "SOURCE_IDENTITY_CANARY"
        unsafe_url = f"https://example.test/job?token={marker}"
        for original, projected, value in (
            ("id", "posting_id", unsafe_url),
            ("source_job_id", "source_job_id", unsafe_url),
            ("fetched_at", "fetched_at", f"javascript:{marker}"),
            ("source", "source_identifier", f"javascript:{marker}"),
            ("source", "source_identifier", f"data:{marker}"),
            ("source_job_id", "source_job_id", "x" * 129),
            ("id", "posting_id", "\ud800"),
        ):
            with self.subTest(field=original, value_type=type(value).__name__):
                row = posting({}, **{original: value})
                envelope = metadata.source_metadata_view(row)
                self.assertIsNone(envelope["source"][projected])
                self.assertEqual(envelope["status"], "PARTIAL")
                self.assertEqual(envelope["omissions"]["source_identity_fields"], 1)
                self.assertIn("SOURCE_IDENTITY_OMITTED", envelope["reason_codes"])
                self.assertNotIn(marker, json.dumps(envelope))
                self.assertEqual(row[original], value)
        missing = metadata.source_metadata_view(posting({}, source_job_id=None))
        self.assertEqual(missing["omissions"]["source_identity_fields"], 0)
        self.assertEqual(missing["status"], "COMPLETE")

    def test_unsafe_url_forms_are_not_exported(self):
        urls = [
            "https://example.test/%2574oken/private", "javascript:alert(1)",
            "https://example.test/path#private", "https://example.test:invalid/path",
            "http://[invalid/path", "https://example.test\\@other.test/path",
            "https://person:private@example.test/path", "//person:private@example.test/path",
            " https://example.test/path?token=private", "\thttps://example.test/path?token=private",
            " //example.test/path?token=private", " javascript:alert(1)",
            "See https://example.test/apply?token=private for details",
            '<a href="https://example.test/apply?access_token=private">Apply</a>',
            '<a href="https:&#47;&#47;example.test/apply?token=private">Apply</a>',
        ]
        for url in urls:
            with self.subTest(url=url):
                envelope = metadata.source_metadata_view(posting({"vendor_link": url}))
                self.assertEqual(envelope["status"], "PARTIAL")
                self.assertEqual(envelope["extensions"], {})
                self.assertEqual(envelope["omissions"]["unsafe_urls"], 1)

    def test_safe_prose_and_html_remain_unchanged(self):
        value = '<p>Read https://example.test/jobs or <a href="https://example.test/team">our team</a>.</p>'
        envelope = metadata.source_metadata_view(posting({"info": value}))
        self.assertEqual(envelope["status"], "COMPLETE")
        self.assertEqual(envelope["extensions"]["info"], value)

    def test_unsafe_url_keys_are_omitted_without_losing_safe_neighbors(self):
        marker = "URL_KEY_CANARY"
        unsafe_keys = [
            f"https://jobs.example.test/apply?sig={marker}",
            f"https://person:{marker}@jobs.example.test/apply",
            f"https://jobs.example.test/apply#{marker}",
            f'<a href="https:&#47;&#47;jobs.example.test/apply?sig={marker}">Apply</a>',
        ]
        public_key = "https://jobs.example.test/openings/123"
        public_value = {"title": "Développeur", "open": False}
        for unsafe_key in unsafe_keys:
            with self.subTest(unsafe_key=unsafe_key):
                raw = {"vendor": [{"links_by_url": {
                    unsafe_key: {"label": "Apply"}, public_key: deepcopy(public_value),
                }}]}
                before = deepcopy(raw)
                envelope = metadata.source_metadata_view(posting(raw))
                self.assertEqual(envelope["extensions"], {
                    "vendor": [{"links_by_url": {public_key: public_value}}],
                })
                self.assertEqual(envelope["status"], "PARTIAL")
                self.assertEqual(envelope["omissions"], {
                    "private_fields": 0, "unsafe_urls": 1, "source_identity_fields": 0,
                })
                self.assertEqual(envelope["reason_codes"], ["UNSAFE_URLS_OMITTED"])
                self.assertNotIn(marker, json.dumps(envelope))
                self.assertEqual(raw, before)

    def test_maximum_length_url_free_text_is_preserved(self):
        value = "a" * metadata.MAX_STRING_BYTES
        envelope = metadata.source_metadata_view(posting({"info": value}))
        self.assertEqual(envelope["status"], "COMPLETE")
        self.assertEqual(envelope["extensions"]["info"], value)

    def test_invalid_metadata_is_unavailable_without_a_partial_prefix(self):
        invalid = [None, [], {1: "value"}, {"bad": float("nan")}, {"bad": float("inf")},
                   {"bad": object()}, {"bad": "\ud800"}, {"bad\nkey": "value"}]
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                envelope = metadata.source_metadata_view(posting(value))
                self.assertEqual(envelope["status"], "UNAVAILABLE")
                self.assertEqual(envelope["extensions"], {})
                self.assertTrue(envelope["reason_codes"])
                json.dumps(envelope, allow_nan=False).encode("utf-8")

    def test_depth_collection_and_string_limits_preserve_the_raw_input(self):
        deep = {}
        current = deep
        for _ in range(metadata.MAX_DEPTH + 2):
            current["child"] = {}
            current = current["child"]
        oversized = [deep, {"items": [0] * (metadata.MAX_COLLECTION_ITEMS + 1)},
                     {"value": "x" * (metadata.MAX_STRING_BYTES + 1)},
                     {"value": 1 << metadata.MAX_INTEGER_BITS}]
        for value in oversized:
            before = deepcopy(value)
            envelope = metadata.source_metadata_view(posting(value))
            self.assertEqual(envelope["status"], "UNAVAILABLE")
            self.assertIn("METADATA_LIMIT_EXCEEDED", envelope["reason_codes"])
            self.assertEqual(envelope["extensions"], {})
            self.assertEqual(value, before)

    def test_aggregate_nodes_and_encoded_bytes_are_bounded(self):
        many_nodes = {str(index): [0] * 512 for index in range(9)}
        escaped_bytes = {str(index): "\n" * 50000 for index in range(3)}
        for value in (many_nodes, escaped_bytes):
            envelope = metadata.source_metadata_view(posting(value))
            self.assertEqual(envelope["status"], "UNAVAILABLE")
            self.assertIn("METADATA_LIMIT_EXCEEDED", envelope["reason_codes"])

    def test_empty_metadata_is_complete_and_not_missing(self):
        envelope = metadata.source_metadata_view(posting({}))
        self.assertEqual(envelope["status"], "COMPLETE")
        self.assertEqual(envelope["extensions"], {})
        self.assertEqual(envelope["reason_codes"], [])

    def test_projection_is_deterministic_and_does_not_infer_provider_or_confidence(self):
        row = posting({"provider": "claimed-provider", "confidence": 1, "new_field": 5})
        first = metadata.source_metadata_view(row)
        self.assertEqual(first, metadata.source_metadata_view(row))
        self.assertNotIn("provider", first["source"])
        self.assertNotIn("confidence", first)
        self.assertEqual(first["extensions"]["confidence"], 1)


if __name__ == "__main__":
    unittest.main()
