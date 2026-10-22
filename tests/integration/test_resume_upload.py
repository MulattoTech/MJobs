"""Uploaded resumes stay local, bounded, unreviewed, and integrity checked."""
from io import BytesIO
from pathlib import Path
import struct
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

    def test_pdf_upload_rejects_embedded_windows_executable_but_accepts_normal_pdf(self):
        # A PE image can be appended to an otherwise PDF-looking file and still
        # pass the current header/EOF-only check. Use a minimally valid PE32+
        # image so executable detection is tested against more than an MZ/PE
        # signature-shaped byte sequence.
        dos_header = bytearray(64)
        dos_header[:2] = b"MZ"
        struct.pack_into("<I", dos_header, 0x3C, 0x40)
        coff_header = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
        optional_header = bytearray(0xF0)
        struct.pack_into("<H", optional_header, 0, 0x020B)  # PE32+
        struct.pack_into("<I", optional_header, 20, 0x1000)  # BaseOfCode
        struct.pack_into("<Q", optional_header, 24, 0x140000000)  # ImageBase
        struct.pack_into("<II", optional_header, 32, 0x1000, 0x200)  # alignments
        struct.pack_into("<I", optional_header, 56, 0x2000)  # SizeOfImage
        struct.pack_into("<I", optional_header, 60, 0x200)  # SizeOfHeaders
        struct.pack_into("<HH", optional_header, 68, 3, 0x8160)  # subsystem, DLL flags
        struct.pack_into("<QQQQ", optional_header, 72, 0x100000, 0x1000, 0x100000, 0x1000)
        struct.pack_into("<I", optional_header, 108, 16)  # NumberOfRvaAndSizes
        section_header = struct.pack(
            "<8sIIIIIIHHI", b".text", 0x200, 0x1000, 0x200, 0x200,
            0, 0, 0, 0, 0x60000020,
        )
        section_raw_padding = 0x90
        executable = (bytes(dos_header) + b"PE\0\0" + coff_header +
                      bytes(optional_header) + section_header + b"\0" * section_raw_padding +
                      b"\xC3" + b"\0" * 0x1FF)

        pe_offset = struct.unpack_from("<I", executable, 0x3C)[0]
        machine, section_count, _, _, _, optional_size, _ = struct.unpack_from(
            "<HHIIIHH", executable, pe_offset + 4)
        self.assertEqual(machine, 0x8664)
        self.assertEqual(section_count, 1)
        self.assertEqual(optional_size, 0xF0)
        self.assertEqual(struct.unpack_from("<H", executable, pe_offset + 24)[0], 0x020B)
        section_table_offset = pe_offset + 4 + 20 + optional_size
        raw_offset = struct.unpack_from("<I", executable, section_table_offset + 20)[0]
        self.assertEqual(raw_offset, 0x200)
        self.assertEqual(raw_offset, section_table_offset + 40 + section_raw_padding)
        polyglot = b"%PDF-1.4\nSynthetic document\n" + executable + b"\n%%EOF\n"

        ordinary = self.upload(b"%PDF-1.4\nSynthetic document\n%%EOF\n", "plain.pdf", "application/pdf")
        self.assertEqual(ordinary.status_code, 201, ordinary.text)
        rejected = self.upload(polyglot, "polyglot.pdf", "application/pdf")
        self.assertIn(rejected.status_code, (400, 415, 422), rejected.text)
        self.assertEqual(len(self.client.get("/profiles/fixture/resumes").json()["resumes"]), 1)

    def test_duplicate_bytes_keep_first_authoritative_extension_and_metadata(self):
        for first_name, first_media, second_name, second_media in (
            ("original.pdf", "application/pdf", "same-bytes.txt", "text/plain"),
            ("original.txt", "text/plain", "same-bytes.pdf", "application/pdf"),
        ):
            with self.subTest(first=first_name):
                data = b"%PDF-1.4\nSynthetic ASCII resume " + first_name.encode() + b"\n%%EOF\n"
                before_count = len(self.client.get("/profiles/fixture/resumes").json()["resumes"])
                first = self.upload(data, first_name, first_media)
                self.assertEqual(first.status_code, 201, first.text)
                original = first.json()
                second = self.upload(data, second_name, second_media)
                self.assertEqual(second.status_code, 201, second.text)
                self.assertEqual(second.json()["id"], original["id"])
                self.assertEqual(second.json()["filename"], original["filename"])
                self.assertEqual(second.json()["format"], original["format"])
                self.assertEqual(second.json()["media_type"], original["media_type"])
                self.assertEqual(len(self.client.get("/profiles/fixture/resumes").json()["resumes"]), before_count + 1)
                stored = list((self.root / "uploads").glob(original["id"] + ".*"))
                self.assertEqual([path.name for path in stored], [original["id"] + "." + original["format"]])
                self.assertEqual(self.client.get("/profiles/fixture/resumes/" + original["id"]).content, data)
                stored[0].write_bytes(b"corrupted original")
                self.assertEqual(self.upload(data, second_name, second_media).status_code, 409)
                self.assertEqual(len(list((self.root / "uploads").glob(original["id"] + ".*"))), 1)

    def test_concurrent_mixed_extension_uploads_have_one_original(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from services.documents.uploads import save_resume

        barrier = Barrier(2)
        data = b"%PDF-1.4\nConcurrent synthetic resume\n%%EOF\n"

        def upload(extension, media):
            barrier.wait(timeout=5)
            return save_resume(self.store, self.root, 'fixture', 'original.' + extension, media, data)

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(upload, 'pdf', 'application/pdf'),
                       pool.submit(upload, 'txt', 'text/plain')]
            first, second = [future.result(timeout=10) for future in pending]
        self.assertEqual(first, second)
        self.assertEqual(first['version'], 1)
        self.assertEqual([p.name for p in (self.root / 'uploads').iterdir()],
                         [first['id'] + '.' + first['format']])
        self.assertEqual(len(self.client.get('/profiles/fixture/resumes').json()['resumes']), 1)

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
