"""Existing uploaded originals require private POSIX storage before reuse or read."""
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from services.documents import uploads

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


CONTENT = b"Synthetic Contact\nsynthetic@example.invalid\nSynthetic employment history\n"
PRIVATE_ERROR = "Uploaded resume storage is not private"


class ResumeUploadPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-upload-privacy-")
        self.root = Path(self.temporary.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temporary.cleanup()

    def upload(self):
        return self.client.post(
            "/profiles/fixture/resumes",
            content=CONTENT,
            headers={"X-Filename": "synthetic-private-resume.txt", "Content-Type": "text/plain"},
        )

    def rows(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in (m.uploaded_resumes, m.resume_revisions)
            }

    def seed(self, *, directory_mode, file_mode):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        path = self.root / "uploads" / (record["id"] + ".txt")
        path.parent.chmod(directory_mode)
        path.chmod(file_mode)
        return record, path

    def assert_refused_without_mutation(self, *, directory_mode, file_mode, read):
        record, path = self.seed(directory_mode=directory_mode, file_mode=file_mode)
        before_rows = self.rows()
        before_facts = self.store.profile("fixture")["facts"]
        before_bytes = path.read_bytes()
        before_names = sorted(item.name for item in path.parent.iterdir())
        with patch.object(uploads.os, "fdopen", side_effect=AssertionError(
                "Unsafe originals must be rejected before a stream can consume bytes")) as reader:
            if read:
                reopened = Store(self.url)
                try:
                    with TestClient(create_app(reopened, "http://127.0.0.1:8090", self.root)) as client:
                        response = client.get("/profiles/fixture/resumes/" + record["id"])
                finally:
                    reopened.engine.dispose()
            else:
                response = self.upload()
        reader.assert_not_called()
        # Check preservation before the refusal assertion, including on the baseline.
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), file_mode)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), directory_mode)
        self.assertEqual(sorted(item.name for item in path.parent.iterdir()), before_names)
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self.store.profile("fixture")["facts"], before_facts)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": PRIVATE_ERROR})
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("synthetic@example.invalid", response.text)

    @unittest.skipUnless(os.name == "posix", "POSIX directory permission semantics required")
    def test_reopened_read_refuses_permissive_directory_without_mutation(self):
        self.assert_refused_without_mutation(directory_mode=0o755, file_mode=0o600, read=True)

    @unittest.skipUnless(os.name == "posix", "POSIX directory permission semantics required")
    def test_repeat_upload_refuses_permissive_directory_without_mutation(self):
        self.assert_refused_without_mutation(directory_mode=0o755, file_mode=0o600, read=False)

    @unittest.skipUnless(os.name == "posix", "POSIX file permission semantics required")
    def test_reopened_read_refuses_permissive_original_without_mutation(self):
        self.assert_refused_without_mutation(directory_mode=0o700, file_mode=0o644, read=True)

    @unittest.skipUnless(os.name == "posix", "POSIX file permission semantics required")
    def test_repeat_upload_refuses_permissive_original_without_mutation(self):
        self.assert_refused_without_mutation(directory_mode=0o700, file_mode=0o644, read=False)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_fresh_upload_is_private_under_permissive_umask(self):
        previous = os.umask(0)
        try:
            response = self.upload()
        finally:
            os.umask(previous)
        self.assertEqual(response.status_code, 201, response.text)
        path = self.root / "uploads" / (response.json()["id"] + ".txt")
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), CONTENT)
        self.assertEqual(len(self.rows()["uploaded_resumes"]), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX read-only file permissions required")
    def test_private_read_only_original_remains_readable_and_reusable(self):
        record, path = self.seed(directory_mode=0o700, file_mode=0o400)
        before = self.rows()
        self.assertEqual(self.client.get("/profiles/fixture/resumes/" + record["id"]).content, CONTENT)
        repeat = self.upload()
        self.assertEqual(repeat.status_code, 201, repeat.text)
        self.assertEqual(repeat.json()["id"], record["id"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link semantics required")
    def test_private_original_with_two_links_remains_readable_and_reusable(self):
        record, path = self.seed(directory_mode=0o700, file_mode=0o600)
        alias = path.parent / ".upload-synthetic-active-staging"
        os.link(path, alias)
        self.assertEqual(path.stat().st_nlink, 2)
        before = self.rows()
        try:
            self.assertEqual(self.client.get("/profiles/fixture/resumes/" + record["id"]).content, CONTENT)
            response = self.upload()
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["id"], record["id"])
            self.assertEqual(self.rows(), before)
            self.assertEqual(path.stat().st_nlink, 2)
        finally:
            alias.unlink()

    def test_native_private_original_roundtrip_and_repeat_preserve_metadata(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        before = self.rows()
        repeated = self.upload()
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(repeated.json(), record)
        self.assertEqual(self.client.get("/profiles/fixture/resumes/" + record["id"]).content, CONTENT)
        self.assertEqual(self.rows(), before)

    def test_storage_open_failure_is_sanitized_and_preserves_metadata(self):
        from unittest.mock import patch
        from services.documents import uploads

        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        before = self.rows()
        with patch.object(uploads.os, "open", side_effect=PermissionError("synthetic-private-storage-path")):
            response = self.client.get("/profiles/fixture/resumes/" + record["id"])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "Uploaded resume storage path is unavailable or unsafe"})
        self.assertNotIn("synthetic-private-storage-path", response.text)
        self.assertEqual(self.rows(), before)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_cleanup_failure_preserves_private_admission_error(self):
        from unittest.mock import patch

        record, path = self.seed(directory_mode=0o700, file_mode=0o644)
        before = self.rows()
        original_unlink = Path.unlink

        def refused_cleanup(target, *args, **kwargs):
            if target.name.startswith(".upload-"):
                raise PermissionError("synthetic-private-cleanup-path")
            return original_unlink(target, *args, **kwargs)

        with patch.object(Path, "unlink", refused_cleanup):
            response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": PRIVATE_ERROR})
        self.assertEqual(self.rows(), before)
        self.assertEqual(path.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        temporary = list(path.parent.glob(".upload-*"))
        self.assertEqual(len(temporary), 1)
        self.assertEqual(stat.S_IMODE(temporary[0].stat().st_mode), 0o600)
        self.assertEqual(temporary[0].read_bytes(), CONTENT)
        temporary[0].unlink()

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX no-follow open required")
    def test_final_symlink_swap_is_refused_before_read(self):
        from unittest.mock import patch
        from services.documents import uploads

        record, path = self.seed(directory_mode=0o700, file_mode=0o600)
        unrelated = self.root / "unrelated-synthetic-original.txt"
        unrelated.write_bytes(CONTENT)
        unrelated.chmod(0o600)
        before = self.rows()
        original_open = os.open
        swapped = []

        def swap_before_open(target, flags, *args, **kwargs):
            if target == path.name and kwargs.get("dir_fd") is not None:
                path.unlink()
                path.symlink_to(unrelated)
                swapped.append(True)
            return original_open(target, flags, *args, **kwargs)

        with patch.object(uploads.os, "open", side_effect=swap_before_open):
            response = self.client.get("/profiles/fixture/resumes/" + record["id"])
        self.assertTrue(swapped)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "Uploaded resume storage path is unavailable or unsafe"})
        self.assertEqual(self.rows(), before)
        self.assertEqual(unrelated.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o600)
        path.unlink()

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "POSIX FIFO semantics required")
    def test_fifo_original_is_refused_without_blocking_or_metadata_change(self):
        import json
        import subprocess
        import sys

        record, path = self.seed(directory_mode=0o700, file_mode=0o600)
        before = self.rows()
        path.unlink()
        os.mkfifo(path, 0o600)
        script = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from apps.api.main import create_app
from packages.persistence.store import Store
from services.documents import uploads
store = Store(sys.argv[1])
try:
    with patch.object(uploads.os, "fdopen", side_effect=AssertionError(
            "FIFO must be refused before a stream can consume bytes")) as reader:
        with TestClient(create_app(store, "http://127.0.0.1:8090", Path(sys.argv[2]))) as client:
            response = client.get("/profiles/fixture/resumes/" + sys.argv[3])
        reader.assert_not_called()
    print(json.dumps({"status": response.status_code, "body": response.json()}))
finally:
    store.engine.dispose()
"""
        result = subprocess.run(
            [sys.executable, "-c", script, self.url, str(self.root), record["id"]],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": 409,
            "body": {"detail": "Uploaded resume storage path is unavailable or unsafe"},
        })
        self.assertEqual(self.rows(), before)
        self.assertTrue(stat.S_ISFIFO(path.stat().st_mode))
        path.unlink()

    def test_invalid_recorded_sizes_are_refused_before_original_open(self):
        from unittest.mock import patch
        from services.documents import uploads

        for invalid in (True, 0, -1, uploads.MAX_UPLOAD_BYTES + 1, "73"):
            with self.subTest(size=invalid), patch.object(uploads.os, "open") as opened:
                with self.assertRaisesRegex(uploads.UploadIntegrityError, "^Uploaded resume size changed$"):
                    uploads._read_original(self.root, "unused-synthetic.txt", invalid, "0" * 64)
                opened.assert_not_called()

    def test_original_filename_must_be_one_safe_component_before_open(self):
        for invalid in ("../outside.txt", "nested/inside.txt", "..\\outside.txt",
                        "C:outside.txt", "", ".", "..", "unsafe\0.txt"):
            with self.subTest(filename=repr(invalid)), patch.object(uploads.os, "open") as opened:
                with self.assertRaisesRegex(uploads.UploadIntegrityError, "^Uploaded resume storage path is unavailable or unsafe$"):
                    uploads._read_original(self.root, invalid, len(CONTENT), "0" * 64)
                opened.assert_not_called()

    def test_fstat_failure_closes_descriptors_without_reading_or_exposing_paths(self):
        from collections import Counter

        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        before = self.rows()
        original_open, original_close, original_fstat = os.open, os.close, os.fstat
        opened, closed = [], []

        def track_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def track_close(descriptor):
            closed.append(descriptor)
            return original_close(descriptor)

        def refuse_original_stat(descriptor):
            info = original_fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                raise PermissionError("synthetic-private-fstat-path")
            return info

        with patch.object(uploads.os, "open", side_effect=track_open), \
                patch.object(uploads.os, "close", side_effect=track_close), \
                patch.object(uploads.os, "fstat", side_effect=refuse_original_stat), \
                patch.object(uploads.os, "fdopen", side_effect=AssertionError(
                    "An unverified descriptor must not consume bytes")) as reader:
            response = self.client.get("/profiles/fixture/resumes/" + record["id"])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "Uploaded resume storage path is unavailable or unsafe"})
        reader.assert_not_called()
        self.assertTrue(opened)
        self.assertEqual(Counter(opened), Counter(closed))
        self.assertEqual(self.rows(), before)

    def test_directory_creation_failure_is_sanitized_without_metadata(self):
        original_mkdir = Path.mkdir

        def refuse_upload_directory(path, *args, **kwargs):
            if path == self.root / "uploads":
                raise PermissionError("synthetic-private-directory-path")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", refuse_upload_directory):
            response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "Uploaded resume storage path is unavailable or unsafe"})
        self.assertEqual(self.rows(), {"uploaded_resumes": [], "resume_revisions": []})
        self.assertFalse((self.root / "uploads").exists())

    def test_download_and_duplicate_original_reads_are_bounded(self):
        from contextlib import contextmanager
        from types import SimpleNamespace

        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        before = self.rows()
        original_fdopen = os.fdopen
        reads = []

        @contextmanager
        def observed_stream(*args, **kwargs):
            with original_fdopen(*args, **kwargs) as stream:
                def read(size):
                    reads.append(size)
                    return stream.read(size)
                yield SimpleNamespace(read=read)

        with patch.object(uploads.os, "fdopen", side_effect=observed_stream):
            downloaded = self.client.get("/profiles/fixture/resumes/" + record["id"])
            repeated = self.upload()
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, CONTENT)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(reads, [len(CONTENT) + 1, len(CONTENT) + 1])
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":
    unittest.main()
