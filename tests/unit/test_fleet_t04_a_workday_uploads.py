"""Workday's bounded upload contract accepts immutable snapshots only."""
import hashlib
import unittest

from services.applications.upload_integrity import (
    MAX_PREPARED_BYTES,
    UploadSnapshot,
    snapshot_bytes,
)
from services.applications.adapters.workday_uploads import preflight_workday_uploads


class WorkdayUploadPreflightTests(unittest.TestCase):
    def snapshot(self, kind="resume", content=b"%PDF-synthetic", **changes):
        values = {
            "kind": kind,
            "document_id": f"synthetic-{kind}",
            "filename": f"{kind}.pdf",
            "media_type": "application/pdf",
            "content": content,
            "expected_sha256": hashlib.sha256(content).hexdigest(),
        }
        values.update(changes)
        return snapshot_bytes(**values)

    def test_required_resume_returns_digest_only_manifest(self):
        resume = self.snapshot()

        result = preflight_workday_uploads(
            [{"kind": "resume", "required": True, "ambiguous": False}],
            [resume],
        )

        self.assertEqual(result, [resume.manifest()])
        self.assertNotIn("filename", result[0])
        self.assertNotIn("content", result[0])

    def test_optional_cover_letter_may_be_absent(self):
        resume = self.snapshot()
        fields = [
            {"kind": "resume", "required": True, "ambiguous": False},
            {"kind": "cover-letter", "required": False, "ambiguous": False},
        ]
        self.assertEqual(preflight_workday_uploads(fields, [resume]), [resume.manifest()])

    def test_required_and_unmatched_fields_fail_closed(self):
        resume = self.snapshot()
        cases = [
            ([{"kind": "resume", "required": True, "ambiguous": False}], [],
             "WORKDAY_UPLOAD_REQUIRED_MISSING"),
            ([{"kind": "resume", "required": True, "ambiguous": False}],
             [self.snapshot("cover-letter", b"%PDF-letter")], "WORKDAY_UPLOAD_SET_INVALID"),
            ([{"kind": "resume", "required": True, "ambiguous": False}],
             [resume, resume], "WORKDAY_UPLOAD_SET_INVALID"),
        ]
        for fields, snapshots, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                preflight_workday_uploads(fields, snapshots)

    def test_ambiguous_duplicate_and_unknown_fields_fail_closed(self):
        resume = self.snapshot()
        cases = [
            [{"kind": "resume", "required": True, "ambiguous": True}],
            [
                {"kind": "resume", "required": True, "ambiguous": False},
                {"kind": "resume", "required": False, "ambiguous": False},
            ],
            [{"kind": "other", "required": False, "ambiguous": False}],
        ]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(
                ValueError, "WORKDAY_UPLOAD_FIELDS_INVALID"
            ):
                preflight_workday_uploads(fields, [resume])

    def test_tampered_or_oversized_snapshot_fails_closed(self):
        valid = self.snapshot()
        tampered = UploadSnapshot(
            kind=valid.kind, document_id=valid.document_id,
            filename=valid.filename, media_type=valid.media_type,
            content=b"%PDF-changed", sha256=valid.sha256,
        )
        oversized_bytes = b"%PDF-" + b"x" * MAX_PREPARED_BYTES
        oversized = UploadSnapshot(
            kind="resume", document_id="synthetic-resume", filename="resume.pdf",
            media_type="application/pdf", content=oversized_bytes,
            sha256=hashlib.sha256(oversized_bytes).hexdigest(),
        )
        fields = [{"kind": "resume", "required": True, "ambiguous": False}]
        for snapshot in (tampered, oversized):
            with self.subTest(size=len(snapshot.content)), self.assertRaisesRegex(
                ValueError, "WORKDAY_UPLOAD_SNAPSHOT_INVALID"
            ):
                preflight_workday_uploads(fields, [snapshot])


if __name__ == "__main__":
    unittest.main()
