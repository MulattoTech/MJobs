"""Uploaded resumes stay local, bounded, unreviewed, and integrity checked."""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from docx import Document
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class ResumeUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temporary.cleanup()

    def upload(self, content: bytes, filename: str, media: str):
        return self.client.post("/profiles/fixture/resumes", content=content,
                                headers={"X-Filename": filename, "Content-Type": media})

    def test_pdf_upload_is_persistent_unreviewed_and_integrity_checked(self):
        data = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
        before = self.store.profile("fixture")["facts"]
        response = self.upload(data, "Base Resume.pdf", "application/pdf")
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(item["status"], "STORED_UNREVIEWED")
        self.assertEqual(item["filename"], "Base Resume.pdf")
        self.assertEqual(item["size_bytes"], len(data))
        self.assertEqual(self.store.profile("fixture")["facts"], before)
        self.assertEqual(self.upload(data, "Another Name.pdf", "application/pdf").json()["id"], item["id"])
        self.assertEqual(len(self.client.get("/profiles/fixture/resumes").json()["resumes"]), 1)
        self.assertEqual(self.client.get("/profiles/fixture/resumes/" + item["id"]).content, data)
        self.assertIn("attachment", self.client.get("/profiles/fixture/resumes/" + item["id"]).headers["content-disposition"])
        reopened = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        try:
            with TestClient(create_app(reopened, "http://127.0.0.1:8090", self.root)) as after_restart:
                self.assertEqual(after_restart.get("/profiles/fixture/resumes/" + item["id"]).content, data)
        finally:
            reopened.engine.dispose()
        path = self.root / "uploads" / (item["id"] + ".pdf")
        path.write_bytes(b"altered")
        self.assertEqual(self.client.get("/profiles/fixture/resumes/" + item["id"]).status_code, 409)

    def test_pdf_docx_and_txt_are_accepted_only_with_matching_types(self):
        doc = Document()
        doc.add_paragraph("Synthetic employment record")
        stream = BytesIO()
        doc.save(stream)
        for name, media, content in (
            ("base.pdf", "application/pdf", b"%PDF-1.4\n%%EOF"),
            ("base.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", stream.getvalue()),
            ("base.txt", "text/plain", "Synthetic employment record".encode("utf-8")),
        ):
            with self.subTest(name=name):
                response = self.upload(content, name, media)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(self.client.get("/profiles/fixture/resumes/" + response.json()["id"]).content, content)

    def test_unsafe_or_mismatched_uploads_are_rejected_without_storage(self):
        valid = b"%PDF-1.4\n%%EOF"
        for name, media, data in (
            ("../outside.pdf", "application/pdf", valid),
            ("base.exe", "application/octet-stream", b"MZ\x00\x00"),
            ("base.pdf", "text/plain", valid),
            ("base.pdf", "application/pdf", b"MZ\x00\x00"),
            ("base.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04bad"),
            ("base.txt", "text/plain", b"invalid\x00text"),
            ("base.pdf", "application/pdf", b""),
            ("base.pdf", "application/pdf", b"x" * (10 * 1024 * 1024 + 1)),
        ):
            with self.subTest(name=name, media=media, size=len(data)):
                self.assertIn(self.upload(data, name, media).status_code, (400, 413, 415, 422))
        self.assertEqual(self.client.get("/profiles/fixture/resumes").json()["resumes"], [])
        self.assertFalse((self.root / "uploads" / "outside.pdf").exists())

    def test_profile_scope_and_cross_site_read_are_enforced(self):
        second = dict(PROFILE, id="second", email="second@example.invalid")
        self.store.save_profile(second)
        item = self.upload(b"Synthetic\n", "base.txt", "text/plain").json()
        self.assertEqual(self.client.get("/profiles/second/resumes/" + item["id"]).status_code, 404)
        self.assertEqual(self.client.get("/profiles/fixture/resumes", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.client.post("/profiles/missing/resumes", content=b"Synthetic\n",
                                          headers={"X-Filename": "base.txt", "Content-Type": "text/plain"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
