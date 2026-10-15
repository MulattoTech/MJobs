"""Fail-closed classification for generated document outputs."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document

from services.documents.failure_status import (
    DOCUMENT_GENERATION_FAILED,
    failure_status,
    validate_generated_outputs,
)
from services.documents import generate
from services.documents.errors import PreparationError
from services.documents.render import render_pdf
from workers.operations import OperationCancelled, OperationTimedOut


class DocumentFailureStatusTests(unittest.TestCase):
    def _valid_outputs(self, root: Path):
        paragraphs = ["Synthetic resume", "Synthetic approved experience"]
        paths = {name: root / f"resume.{name}" for name in ("docx", "pdf", "txt")}
        document = Document()
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        document.save(paths["docx"])
        render_pdf(paths["pdf"], paragraphs)
        paths["txt"].write_text("\n".join(paragraphs) + "\n", encoding="utf-8")
        return paths

    def test_complete_integrity_checked_outputs_are_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._valid_outputs(Path(directory))
            hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for name, path in paths.items()}

            result = validate_generated_outputs(paths, hashes)

        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["ready"])
        self.assertEqual(result["formats"], ["docx", "pdf", "txt"])

    def test_missing_format_is_classified_as_generation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._valid_outputs(Path(directory))
            del paths["pdf"]

            with self.assertRaises(ValueError) as caught:
                validate_generated_outputs(paths)

        self.assertEqual(caught.exception.code, DOCUMENT_GENERATION_FAILED)

    def test_corrupt_or_empty_format_is_never_ready(self):
        for corrupt in (b"", b"partial output"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                paths = self._valid_outputs(Path(directory))
                paths["pdf"].write_bytes(corrupt)

                with self.assertRaises(ValueError) as caught:
                    validate_generated_outputs(paths)

                self.assertEqual(caught.exception.code, DOCUMENT_GENERATION_FAILED)

    def test_hash_mismatch_is_classified_as_generation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._valid_outputs(Path(directory))
            hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for name, path in paths.items()}
            paths["txt"].write_text("tampered", encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                validate_generated_outputs(paths, hashes)

        self.assertEqual(caught.exception.code, DOCUMENT_GENERATION_FAILED)

    def test_failure_record_is_stable_and_does_not_expose_exception_text(self):
        result = failure_status(RuntimeError("private path and profile data"))

        self.assertEqual(result, {
            "status": DOCUMENT_GENERATION_FAILED,
            "error_code": DOCUMENT_GENERATION_FAILED,
            "recovery": "REGENERATE",
        })

    def test_partial_atomic_write_failure_is_sanitized_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "resume.pdf"
            destination.write_bytes(b"previous valid revision")

            def write_partial_then_fail(path):
                path.write_bytes(b"partial private output")
                raise OSError("private path and profile details")

            with self.assertRaises(PreparationError) as caught:
                generate._generate_document_artifact(destination, write_partial_then_fail)

            self.assertEqual(caught.exception.code, DOCUMENT_GENERATION_FAILED)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(destination.read_bytes(), b"previous valid revision")
            self.assertEqual(list(root.glob(".resume.pdf.*.tmp")), [])

    def test_artifact_generation_preserves_control_and_preparation_errors(self):
        errors = [
            PreparationError("DOCUMENT_FONT_UNAVAILABLE", "font recovery remains distinct"),
            OperationCancelled("cancel request remains distinct"),
            OperationTimedOut("deadline remains distinct"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                with patch.object(generate, "atomic_generate", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        generate._generate_document_artifact(Path(directory) / "resume.pdf", lambda _path: None)

                self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
