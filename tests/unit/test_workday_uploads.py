"""Pure Workday upload preflight contracts use synthetic document bytes only."""
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

    def test_resume_slot_passes_without_a_cover_letter(self):
        resume = self.snapshot()

        result = preflight_workday_uploads(
            [{"kind": "resume", "required": True, "ambiguous": False}],
            [resume],
        )

        self.assertEqual(result, [resume.manifest()])

    def test_optional_cover_letter_mapping_may_be_absent(self):
        resume = self.snapshot()

        result = preflight_workday_uploads(
            [
                {"kind": "resume", "required": True, "ambiguous": False},
                {"kind": "cover-letter", "required": False, "ambiguous": False},
            ],
            [resume],
        )

        self.assertEqual(result, [resume.manifest()])

    def test_unmatched_or_duplicate_snapshots_fail_closed(self):
        resume = self.snapshot()
        letter = self.snapshot("cover-letter", b"%PDF-letter")
        fields = [{"kind": "resume", "required": True, "ambiguous": False}]

        for snapshots in ([letter], [resume, letter], [resume, resume]):
            with self.subTest(snapshots=[item.kind for item in snapshots]), self.assertRaisesRegex(
                ValueError, "WORKDAY_UPLOAD_SET_INVALID"
            ):
                preflight_workday_uploads(fields, snapshots)

    def test_duplicate_ambiguous_and_unknown_slots_fail_closed(self):
        resume = self.snapshot()
        for fields in (
            [
                {"kind": "resume", "required": True, "ambiguous": False},
                {"kind": "resume", "required": False, "ambiguous": False},
            ],
            [{"kind": "resume", "required": True, "ambiguous": True}],
            [{"kind": "other", "required": False, "ambiguous": False}],
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(
                ValueError, "WORKDAY_UPLOAD_FIELDS_INVALID"
            ):
                preflight_workday_uploads(fields, [resume])

    def test_missing_required_resume_fails_but_no_slots_needs_no_snapshot(self):
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_REQUIRED_MISSING"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": True, "ambiguous": False}], []
            )

        self.assertEqual(preflight_workday_uploads([], []), [])

    def test_present_optional_resume_slot_still_requires_its_snapshot(self):
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_REQUIRED_MISSING"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": False, "ambiguous": False}], []
            )

    def test_snapshot_digest_and_type_constraints_are_revalidated(self):
        valid = self.snapshot()
        tampered = UploadSnapshot(
            kind=valid.kind,
            document_id=valid.document_id,
            filename=valid.filename,
            media_type=valid.media_type,
            content=b"%PDF-modified",
            sha256=valid.sha256,
        )
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_SNAPSHOT_INVALID"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": True, "ambiguous": False}], [tampered]
            )

        unsupported = UploadSnapshot(
            kind="resume",
            document_id="synthetic-resume",
            filename="resume.html",
            media_type="text/html",
            content=b"<html>synthetic</html>",
            sha256=hashlib.sha256(b"<html>synthetic</html>").hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_SNAPSHOT_INVALID"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": True, "ambiguous": False}], [unsupported]
            )

    def test_oversized_snapshot_is_rejected(self):
        content = b"%PDF-" + b"x" * MAX_PREPARED_BYTES
        oversized = UploadSnapshot(
            kind="resume",
            document_id="synthetic-resume",
            filename="resume.pdf",
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_SNAPSHOT_INVALID"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": True, "ambiguous": False}], [oversized]
            )


if __name__ == "__main__":
    unittest.main()
