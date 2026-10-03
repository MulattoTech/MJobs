import io
import unittest
from zipfile import ZipFile

from services.career_profile.extraction import extract_resume


class ResumeExtractionTests(unittest.TestCase):
    def test_txt_extracts_contact_sections_and_split_skills(self):
        data = b"""Jane Q. Example\njane@example.com | +1 (312) 555-0199\nLinkedIn: https://www.linkedin.com/in/jane-example\nChicago, IL 60601\n\nSUMMARY\nPlatform engineer with 10 years of experience.\n\nSKILLS\nPython, PostgreSQL; Docker | Kubernetes\n\nEXPERIENCE\n2020 - Present -- Senior Engineer\nBuilt durable systems and reduced incidents by 30%.\n\nEDUCATION\nB.S. Computer Science, State University, 2014\n"""
        result = extract_resume(data, "txt", "base-resume")

        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(result["contact"]["email"], "jane@example.com")
        self.assertEqual(result["contact"]["phones"], ["+1 (312) 555-0199"])
        self.assertEqual(result["contact"]["linkedin"], "https://www.linkedin.com/in/jane-example")
        self.assertEqual(result["contact"]["city"], "Chicago")
        self.assertEqual(result["contact"]["state"], "IL")
        skill_text = [fact["text"] for fact in result["facts"] if fact["kind"] == "skill"]
        self.assertEqual(skill_text, ["Python", "PostgreSQL", "Docker", "Kubernetes"])
        self.assertTrue(any(f["kind"] == "experience" and "30%" in f["text"] for f in result["facts"]))
        self.assertTrue(all(f["verified"] is False and f["user_approved"] is False for f in result["facts"]))

    def test_pdf_native_text_is_extracted_and_empty_pdf_needs_ocr(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfWriter

        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(200, 200))
        for y, line in ((180, "Alex Example"), (160, "alex@example.com"), (140, "SKILLS"), (120, "Go, Linux")):
            canvas.drawString(20, y, line)
        canvas.save()
        result = extract_resume(output.getvalue(), "pdf", "uploaded.pdf")
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(result["contact"]["email"], "alex@example.com")

        blank = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(blank)
        ocr_result = extract_resume(blank.getvalue(), "pdf", "scan.pdf")
        self.assertEqual(ocr_result["status"], "NEEDS_OCR")

    def test_docx_native_text_is_extracted(self):
        document_xml = b'''<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Sam Example</w:t></w:r></w:p><w:p><w:r><w:t>sam@example.com</w:t></w:r></w:p><w:p><w:r><w:t>SKILLS</w:t></w:r></w:p><w:p><w:r><w:t>Rust, SQL</w:t></w:r></w:p></w:body></w:document>'''
        content_types = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" />'
        data = io.BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("word/document.xml", document_xml)
        result = extract_resume(data.getvalue(), "docx", "resume.docx")
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(result["contact"]["email"], "sam@example.com")
        self.assertEqual([f["text"] for f in result["facts"] if f["kind"] == "skill"], ["Rust", "SQL"])

    def test_bad_input_is_unreadable_and_source_is_bounded(self):
        result = extract_resume(b"not a document", "pdf", "resume.pdf")
        self.assertEqual(result["status"], "UNREADABLE")
        with self.assertRaises(ValueError):
            extract_resume(b"hello", "txt", "x" * 1001)

    def test_contact_stays_in_header_and_does_not_promote_dates_or_headings(self):
        text = ("SKILLS\nPython\nEXPERIENCE\n2020 - 2024\nSalary 2020123456\n"
                "Email: person@example.com\nLinkedIn: linkedin.com/in/person\n")
        result = extract_resume(text.encode(), "txt", "resume")
        self.assertIsNone(result["contact"]["name"])
        self.assertEqual(result["contact"]["phones"], [])
        self.assertIsNone(result["contact"]["postal_code"])
        self.assertEqual(result["contact"]["linkedin"], "https://linkedin.com/in/person")
        self.assertFalse(any("person@example.com" in fact["text"] for fact in result["facts"]))

    def test_duplicate_facts_are_unique_and_overflow_is_warned(self):
        text = "SKILLS\nPython, Python\n" + ("PROJECTS\n" + ("x" * 10001))
        result = extract_resume(text.encode(), "txt", "resume")
        self.assertEqual(len(result["facts"]), 1)
        self.assertTrue(any("limits" in warning for warning in result["warnings"]))

    def test_invalid_utf8_is_unreadable(self):
        result = extract_resume(b"\xff\xfe", "txt", "resume")
        self.assertEqual(result["status"], "UNREADABLE")

    def test_skills_and_employer_locations_do_not_become_home_address(self):
        result = extract_resume(b"Example Applicant\nSKILLS\nGo, AI\nEXPERIENCE\nBoston, MA 02101", 'txt', 'resume')
        self.assertIsNone(result['contact']['city'])
        self.assertIsNone(result['contact']['postal_code'])

    def test_short_unheaded_summary_is_preserved_after_contact_name(self):
        result = extract_resume(b"Example Applicant\nExperienced systems administrator\nSKILLS\nLinux", 'txt', 'resume')
        self.assertIn('Experienced systems administrator', [f['text'] for f in result['facts']])

    def test_header_salary_or_identifier_is_not_a_phone(self):
        result = extract_resume(b"Example Applicant\nExpected salary 1000000000\nEmployee ID 3456789012\nSkills\nPython", 'txt', 'resume')
        self.assertEqual(result['contact']['phones'], [])

    def test_unheaded_ambiguous_company_is_not_a_home_location(self):
        result = extract_resume(b"Jane Example\nAcme, OK\nBuilt systems", 'txt', 'resume')
        self.assertIsNone(result['contact']['city'])

    def test_large_compressed_docx_part_is_rejected_before_xml_parse(self):
        from zipfile import ZIP_DEFLATED
        stream = io.BytesIO()
        with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document>'+(' ' * (2 * 1024 * 1024))+'</document>')
        with self.assertRaisesRegex(ValueError, 'XML part'):
            extract_resume(stream.getvalue(), 'docx', 'resume')


if __name__ == "__main__":
    unittest.main()
