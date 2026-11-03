import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.verification.har_sanitize import (
    HarError, MAX_BODY, MAX_BYTES, MAX_ENTRIES, main, sanitize_bytes,
    sanitize_har, validate_derivative, write_derivative,
)


SECRET = "seeded-secret-NEVER-PUBLISH"
EMAIL = "synthetic.private@example.invalid"


def fixture():
    return {"log": {"version": "1.2", "creator": {"name": SECRET}, "comment": SECRET,
                    "pages": [{"id": SECRET, "title": EMAIL, "startedDateTime": "2026-11-03T05:00:00Z"}],
                    "entries": [{
                        "startedDateTime": "2026-11-03T05:00:00Z", "time": 52.25, "pageref": SECRET,
                        "serverIPAddress": "192.0.2.5", "_initiator": {"token": SECRET},
                        "request": {"method": "POST", "url": f"https://{SECRET}:{SECRET}@{SECRET}.invalid/users/{EMAIL}?{SECRET}={EMAIL}#" + SECRET,
                                    "headers": [{"name": "Authorization", "value": "Bearer " + SECRET}],
                                    "cookies": [{"name": "session", "value": SECRET}], "bodySize": 34,
                                    "postData": {"mimeType": "application/json", "text": json.dumps({SECRET: {"email": EMAIL, "items": [True, 17, None, SECRET]}})}},
                        "response": {"status": 201, "statusText": SECRET, "headers": [{"name": "Set-Cookie", "value": SECRET}],
                                     "cookies": [{"name": SECRET, "value": SECRET}], "bodySize": 101,
                                     "content": {"size": 101, "mimeType": "application/json", "text": json.dumps({"profile": {"email": EMAIL, "phone": "+1-202-555-0199", SECRET: SECRET}})},
                                     "redirectURL": f"https://{SECRET}.invalid/next?token={SECRET}"},
                        "timings": {"blocked": 0, "send": 1, "wait": 50, "receive": 1.25, "secret": SECRET},
                        "cache": {"comment": SECRET}, "unusual": {SECRET: EMAIL},
                    }]}}


class HarSanitizeTests(unittest.TestCase):
    def test_nested_secrets_pii_headers_urls_and_extensions_never_survive(self):
        raw = json.dumps(fixture()).encode()
        output, manifest = sanitize_bytes(raw)
        for secret in (SECRET, EMAIL, "202-555", "Authorization", "Set-Cookie", "192.0.2.5", "profile", "email"):
            self.assertNotIn(secret, output.decode())
        clean = json.loads(output)
        validate_derivative(clean)
        entry = clean["log"]["entries"][0]
        self.assertEqual(entry["request"]["method"], "POST")
        self.assertEqual(entry["response"]["status"], 201)
        self.assertEqual(entry["timings"]["wait"], 50)
        self.assertEqual(entry["startedDateTime"], "2000-01-01T00:00:00.000Z")
        self.assertEqual(entry["pageref"], clean["log"]["pages"][0]["id"])
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["derivative_sha256"], hashlib.sha256(output).hexdigest())
        self.assertEqual(manifest["capture_status"], "NOT_ATTESTED")

    def test_order_endpoint_aliases_and_relative_timings_are_preserved(self):
        data = fixture()
        second = copy.deepcopy(data["log"]["entries"][0])
        second["startedDateTime"] = "2026-11-03T05:00:02.250Z"
        second["request"]["url"] += "-ignored-fragment"
        second["response"]["status"] = 429
        data["log"]["entries"].append(second)
        clean = sanitize_har(data)["log"]["entries"]
        self.assertEqual([row["response"]["status"] for row in clean], [201, 429])
        self.assertEqual(clean[0]["request"]["url"], clean[1]["request"]["url"])
        self.assertEqual(clean[1]["startedDateTime"], "2000-01-01T00:00:02.250Z")

    def test_distinct_origins_and_routes_remain_distinct_without_identity(self):
        data = fixture()
        row = data["log"]["entries"][0]
        data["log"]["entries"] = [copy.deepcopy(row) for _ in range(3)]
        for item, url in zip(data["log"]["entries"], ["https://one.invalid/api/jobs", "https://one.invalid/api/resumes", "https://two.invalid/api/jobs"]):
            item["request"]["url"] = url
        urls = [item["request"]["url"] for item in sanitize_har(data)["log"]["entries"]]
        self.assertEqual(len(set(urls)), 3)
        self.assertTrue(urls[0].startswith("https://origin-1.invalid/"))
        self.assertEqual(urls[0].split("/")[2], urls[1].split("/")[2])
        self.assertNotEqual(urls[0].split("/")[2], urls[2].split("/")[2])

    def test_forms_multipart_text_encoded_invalid_json_and_unusual_mime(self):
        samples = [
            ({"mimeType": "application/x-www-form-urlencoded", "text": f"{SECRET}={EMAIL}&token={SECRET}"}, "schema-only"),
            ({"mimeType": "multipart/form-data", "text": SECRET + EMAIL}, "omitted-unclassified"),
            ({"mimeType": "multipart/form-data", "params": [{"name": SECRET, "fileName": EMAIL, "value": SECRET}]}, "schema-only"),
            ({"mimeType": "text/plain", "text": SECRET}, "omitted-unclassified"),
            ({"mimeType": "application/json", "text": SECRET, "encoding": "base64"}, "omitted-encoded"),
            ({"mimeType": "application/json", "text": SECRET}, "omitted-invalid"),
            ({"mimeType": SECRET, "text": EMAIL}, "omitted-unclassified"),
        ]
        for body, expected in samples:
            with self.subTest(mode=expected):
                data = fixture()
                data["log"]["entries"][0]["request"]["postData"] = body
                output, _ = sanitize_bytes(json.dumps(data).encode())
                post = json.loads(output)["log"]["entries"][0]["request"]["postData"]
                self.assertEqual(post["_bodyRepresentation"], expected)
                self.assertNotIn(SECRET, output.decode())
                self.assertNotIn(EMAIL, output.decode())

    def test_body_depth_and_field_limits_are_explicit_and_safe(self):
        data = fixture()
        value = {f"{SECRET}{i}": EMAIL for i in range(200)}
        for _ in range(12):
            value = {SECRET: value}
        data["log"]["entries"][0]["response"]["content"]["text"] = json.dumps(value)
        clean, _ = sanitize_bytes(json.dumps(data).encode())
        self.assertIn("truncated", clean.decode())
        self.assertNotIn(SECRET, clean.decode())
        data["log"]["entries"][0]["response"]["content"]["text"] = "x" * (MAX_BODY + 1)
        result = sanitize_har(data)["log"]["entries"][0]["response"]["content"]
        self.assertEqual(result["_bodyRepresentation"], "omitted-size")

    def test_invalid_values_cannot_inject_strings_through_numeric_or_enum_fields(self):
        data = fixture()
        row = data["log"]["entries"][0]
        row["time"] = SECRET
        row["request"]["method"] = SECRET
        row["request"]["bodySize"] = 10 ** 1000
        row["response"]["status"] = True
        row["timings"]["wait"] = float("nan")
        output, _ = sanitize_bytes(json.dumps(data).encode())
        entry = json.loads(output)["log"]["entries"][0]
        self.assertEqual(entry["response"]["status"], 0)
        self.assertEqual(entry["request"]["method"], "UNKNOWN")
        self.assertEqual(entry["request"]["bodySize"], -1)
        self.assertEqual(entry["timings"]["wait"], -1)
        self.assertNotIn(SECRET, output.decode())

    def test_malformed_har_and_oversized_inputs_fail_with_stable_errors(self):
        for raw in (b"not-json" + SECRET.encode(), b"[]", b'{"log":{"entries":null}}', b"\xff"):
            with self.subTest(raw_type=type(raw).__name__):
                with self.assertRaises(HarError) as error:
                    sanitize_bytes(raw)
                self.assertNotIn(SECRET, str(error.exception))
        with self.assertRaisesRegex(HarError, "INPUT_TOO_LARGE"):
            sanitize_bytes(b" " * (MAX_BYTES + 1))
        with self.assertRaisesRegex(HarError, "OVERSIZED"):
            sanitize_har({"log": {"entries": [{}] * (MAX_ENTRIES + 1)}})

    def test_malicious_and_malformed_urls_fail_closed(self):
        for url in (f"file:///{EMAIL}", f"https://host.invalid:bad/{SECRET}", f"javascript:{SECRET}", "https://[bad", SECRET * 1000):
            with self.subTest(scheme=url.split(":")[0][:8]):
                data = fixture()
                data["log"]["entries"][0]["request"]["url"] = url
                clean = sanitize_har(data)["log"]["entries"][0]
                self.assertEqual(clean["request"]["url"], "https://redacted.invalid/")

    def test_repeated_raw_conversion_is_deterministic_and_input_is_unchanged(self):
        data = fixture()
        prior = copy.deepcopy(data)
        raw = json.dumps(data).encode()
        self.assertEqual(sanitize_bytes(raw), sanitize_bytes(raw))
        self.assertEqual(data, prior)
        with self.assertRaisesRegex(HarError, "ALREADY_SANITIZED"):
            sanitize_har(sanitize_har(data))

    def test_validator_rejects_tampered_headers_extensions_body_and_free_strings(self):
        for mutate in (
            lambda d: d["log"].update({"comment": SECRET}),
            lambda d: d["log"]["creator"].update({"name": EMAIL}),
            lambda d: d["log"]["entries"][0]["response"].update({"headers": [{"name": "GET", "value": "POST"}]}),
            lambda d: d["log"]["entries"][0]["response"]["content"].update({"text": json.dumps({"type": SECRET})}),
        ):
            clean = sanitize_har(fixture())
            mutate(clean)
            with self.assertRaisesRegex(HarError, "INVALID_DERIVATIVE"):
                validate_derivative(clean)

    def test_exclusive_publication_and_input_alias_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, manifest = [root / name for name in ("private.har", "safe.json", "manifest.json")]
            raw = json.dumps(fixture()).encode()
            source.write_bytes(raw)
            write_derivative(source, output, manifest)
            first_output = output.read_bytes()
            with self.assertRaisesRegex(HarError, "PUBLICATION"):
                write_derivative(source, output, manifest)
            self.assertEqual(output.read_bytes(), first_output)
            self.assertEqual(source.read_bytes(), raw)
            with self.assertRaisesRegex(HarError, "ALIAS"):
                write_derivative(source, source, manifest)
            self.assertFalse(list(root.glob(".har-sanitized-*")))

    def test_second_artifact_failure_rolls_back_only_own_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, manifest = [root / name for name in ("private.har", "safe.json", "manifest.json")]
            source.write_text(json.dumps(fixture()), encoding="utf-8")
            manifest.write_text("other-worker", encoding="utf-8")
            with self.assertRaises(HarError):
                write_derivative(source, output, manifest)
            self.assertFalse(output.exists())
            self.assertEqual(manifest.read_text(), "other-worker")

    def test_cli_failure_does_not_print_private_path_or_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / (SECRET + ".har")
            source.write_text(SECRET, encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main([str(source), "--output", str(root / "out.json"), "--manifest", str(root / "manifest.json")])
            self.assertEqual(code, 1)
            self.assertEqual(stderr.getvalue(), "HAR_SANITIZATION_FAILED\n")

    def test_no_external_network_or_subprocess_is_needed(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            clean, _ = sanitize_bytes(json.dumps(fixture()).encode())
        self.assertTrue(clean)


if __name__ == "__main__":
    unittest.main()
