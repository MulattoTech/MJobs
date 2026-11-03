"""Existing uploaded originals require private POSIX storage before reuse or read."""
from pathlib import Path
import os
import stat
import tempfile
import unittest

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
        if read:
            reopened = Store(self.url)
            try:
                with TestClient(create_app(reopened, "http://127.0.0.1:8090", self.root)) as client:
                    response = client.get("/profiles/fixture/resumes/" + record["id"])
            finally:
                reopened.engine.dispose()
        else:
            response = self.upload()
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


if __name__ == "__main__":
    unittest.main()
