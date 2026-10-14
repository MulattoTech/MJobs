"""Document export reads remain bounded even when files change after stat."""
from pathlib import Path
import unittest
import hashlib
from unittest.mock import patch

from packages.persistence import models as m
from services.data_export import DataExportError, MAX_EXPORT_BYTES, export_profile_backup
from services.documents.uploads import UploadIntegrityError, read_resume
from tests.unit import test_data_export as fixtures


class DataExportReadBoundsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DataExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.document = self.fixture.output_dir / "application-a/v1/1/resume.txt"

    def export(self):
        return export_profile_backup(self.fixture.store, "profile-a",
                                     output_dir=self.fixture.output_dir)

    def test_generated_document_uses_a_bounded_read(self):
        original_open = Path.open
        read_sizes = []

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size=-1):
                read_sizes.append(size)
                return self.stream.read(size)

        def tracked_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return Reader(stream) if path == self.document else stream

        with patch.object(Path, "open", tracked_open):
            backup = self.export()
        self.assertTrue(backup["document_files"])
        self.assertEqual(len(read_sizes), 1)
        self.assertGreater(read_sizes[0], 0)
        self.assertLessEqual(read_sizes[0], MAX_EXPORT_BYTES + 1)

    def test_growing_file_is_rejected_and_retry_reads_unchanged_database(self):
        original_open = Path.open
        changed = False

        def grow_on_open(path, *args, **kwargs):
            nonlocal changed
            if path == self.document and not changed:
                changed = True
                with original_open(path, "wb") as stream:
                    stream.write(b"x" * (MAX_EXPORT_BYTES + 1))
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", grow_on_open):
            with self.assertRaisesRegex(DataExportError, "EXPORT_TOO_LARGE"):
                self.export()
        self.assertTrue(changed)
        self.document.write_bytes(b"generated resume bytes")
        self.fixture.store.engine.dispose()
        self.assertTrue(self.export()["document_files"])

    def test_uploaded_resume_read_is_limited_to_its_recorded_size(self):
        upload = self.fixture.output_dir / "uploads/upload-a.pdf"
        original_open = Path.open
        sizes = []

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size=-1):
                sizes.append(size)
                return self.stream.read(size)

        def tracked_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return Reader(stream) if path == upload else stream

        with patch.object(Path, "open", tracked_open):
            self.export()
        self.assertEqual(sizes, [len(b"uploaded pdf bytes") + 1])

    def test_document_collection_stops_before_reading_past_total_budget(self):
        pdf = self.document.with_suffix(".pdf")
        content = b"x" * 2100
        self.document.write_bytes(content)
        pdf.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with self.fixture.store.engine.begin() as connection:
            connection.execute(m.documents.update().where(m.documents.c.id == "document-a").values(
                files={"pdf": "v1/1/resume.pdf", "txt": "v1/1/resume.txt"},
                sha256={"pdf": digest, "txt": digest}))
        original_open = Path.open
        opened = []

        def tracked_open(path, *args, **kwargs):
            if path in (pdf, self.document):
                opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch("services.data_export.MAX_EXPORT_BYTES", 5000), patch.object(Path, "open", tracked_open):
            with self.assertRaisesRegex(DataExportError, "EXPORT_TOO_LARGE"):
                self.export()
        self.assertEqual(opened, [pdf])

    def test_uploaded_resume_growth_is_rejected_before_checksum_and_retry_succeeds(self):
        upload = self.fixture.output_dir / "uploads/upload-a.pdf"
        original_open = Path.open
        changed = False

        def grow_on_open(path, *args, **kwargs):
            nonlocal changed
            if path == upload and not changed:
                changed = True
                with original_open(path, "wb") as stream:
                    stream.write(b"x" * 100_000)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", grow_on_open):
            with self.assertRaisesRegex(UploadIntegrityError, "size changed"):
                read_resume(self.fixture.store, self.fixture.output_dir, "profile-a", "upload-a")
        upload.write_bytes(b"uploaded pdf bytes")
        self.assertTrue(self.export()["document_files"])
