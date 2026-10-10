"""Bounded immutable upload snapshot and browser readback contracts."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from services.applications.upload_integrity import (
    UploadIntegrityError, MAX_PREPARED_BYTES, attach_verified_files,
    snapshot_bytes, snapshot_prepared, verify_file_selection,
)


class UploadIntegrityTests(unittest.TestCase):
    def snapshot(self, content=b"%PDF-fixture", **changes):
        args = dict(kind="resume", document_id="doc-1", filename="resume.pdf",
                    media_type="application/pdf", content=content,
                    expected_sha256=hashlib.sha256(content).hexdigest())
        args.update(changes)
        return snapshot_bytes(**args)

    def test_verified_snapshot_is_immutable_and_excludes_filename_from_manifest(self):
        item = self.snapshot()
        with self.assertRaises(AttributeError):
            item.content = b"changed"
        self.assertEqual(set(item.manifest()), {"kind", "document_id", "size_bytes", "sha256"})
        self.assertNotIn("resume.pdf", str(item.manifest()))

    def test_changed_bytes_are_rejected(self):
        with self.assertRaisesRegex(UploadIntegrityError, "CONTENT_CHANGED"):
            self.snapshot(expected_sha256="0" * 64)

    def test_empty_large_or_mutable_buffers_are_rejected(self):
        for content in (b"", b"x" * (MAX_PREPARED_BYTES + 1), bytearray(b"%PDF-fixture")):
            with self.subTest(size=len(content)), self.assertRaisesRegex(UploadIntegrityError, "SIZE_INVALID"):
                self.snapshot(content)

    def test_prepared_non_pdf_and_unsupported_media_types_fail(self):
        with self.assertRaisesRegex(UploadIntegrityError, "PDF_INVALID"):
            self.snapshot(b"not-a-pdf")
        with self.assertRaisesRegex(UploadIntegrityError, "MEDIA_TYPE_INVALID"):
            self.snapshot(kind="supporting-document", media_type="text/html")

    def test_filename_paths_and_controls_are_rejected(self):
        for filename in ("../resume.pdf", "C:\\private.pdf", "bad\nname.pdf", "", "x" * 256):
            with self.subTest(filename=filename), self.assertRaisesRegex(UploadIntegrityError, "FILENAME_INVALID"):
                self.snapshot(filename=filename)

    def test_disk_file_is_frozen_not_reopened_during_attach(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.pdf"
            path.write_bytes(b"%PDF-original")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            item = snapshot_prepared({"id": "doc", "kind": "resume", "sha256": {"pdf": digest}}, path)
            path.write_bytes(b"%PDF-changed")
            locator = Mock()
            locator.evaluate.return_value = [{"name": "resume.pdf", "type": "application/pdf",
                "size": len(item.content), "sha256": digest}]
            attach_verified_files(locator, [item])
            self.assertEqual(locator.set_input_files.call_args.args[0][0]["buffer"], b"%PDF-original")

    def test_exact_browser_count_size_type_name_and_digest_are_required(self):
        item = self.snapshot()
        expected = {"name": item.filename, "type": item.media_type,
                    "size": len(item.content), "sha256": item.sha256}
        for actual in (None, [], [expected, expected], [{**expected, "sha256": "0" * 64}],
                       [{**expected, "size": 1}], [{**expected, "name": "another.pdf"}],
                       [{**expected, "type": "text/plain"}]):
            with self.subTest(actual=actual), self.assertRaisesRegex(UploadIntegrityError, "BROWSER_BYTES_MISMATCH"):
                verify_file_selection(Mock(evaluate=Mock(return_value=actual)), [item])

    def test_cancellation_is_checked_before_any_browser_action(self):
        locator = Mock()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            attach_verified_files(locator, [self.snapshot()], check=Mock(side_effect=RuntimeError("cancelled")))
        locator.set_input_files.assert_not_called()

    def test_excessive_or_empty_file_sets_are_rejected(self):
        for items in ([], [self.snapshot()] * 6, [object()]):
            with self.assertRaisesRegex(UploadIntegrityError, "SET_INVALID"):
                attach_verified_files(Mock(), items)
