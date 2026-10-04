import io
import unittest
from zipfile import ZipFile

from services.career_profile.extraction import extract_resume


class ResumeExtractionTests(unittest.TestCase):
    def test_dated_career_facts_have_review_ranges_without_changed_text_or_approval(self):
        lines = ['Example Co | Engineer | Jan 2020 - December 2023',
                 'Other Co | Administrator | 2024 - Present',
                 'Earlier role | 03/2018 - 2019-12']
        result = extract_resume(('Experience\n'+'\n'.join(lines)).encode(), 'txt', 'resume')
        self.assertEqual([f['text'] for f in result['facts']], lines)
        self.assertEqual([(r['start'], r['end']) for r in result['date_ranges']],
                         [('2020-01', '2023-12'), ('2024', 'PRESENT'), ('2018-03', '2019-12')])
        self.assertEqual([r['fact_id'] for r in result['date_ranges']], [f['id'] for f in result['facts']])
        self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in result['facts']))

    def test_uncertain_dates_remain_original_text_without_inferred_chronology(self):
        lines = ['Engineer 2024 - 2020', 'Engineer 2020 - 2022 and 2023 - Present',
                 'Engineer 03/04/2020 - 05/06/2024', 'Engineer 2020-2021-2022',
                 'Engineer approximately 2020 through the pandemic', 'Engineer 2020-13 - 2024-14',
                 'Identifier 12020-20240', 'Engineer 2024-12 - 2024-01', 'Engineer 2020–2021–2022',
                 'Engineer 2020 - 2021 - 2022', 'Engineer 2019 - 2020.05',
                 'Engineer 2020 - 2021.foo', 'Engineer 2020 - 2021, 2022',
                 'Engineer 2019; 2020 - 2021']
        result = extract_resume(('Experience\n'+'\n'.join(lines)).encode(), 'txt', 'resume')
        self.assertEqual([f['text'] for f in result['facts']], lines)
        self.assertEqual(result['date_ranges'], [])

    def test_responsibilities_have_their_own_pending_fact_type(self):
        result = extract_resume(b'Responsibilities\nMaintain Linux servers.\nAchievements\nReduced incidents.',
                                'txt', 'resume')
        self.assertEqual([(f['kind'], f['text']) for f in result['facts']],
                         [('responsibility', 'Maintain Linux servers.'), ('accomplishment', 'Reduced incidents.')])
        # The previous extractor classified this duty as experience. A saved
        # fact must retain its ID when the same upload is extracted again.
        legacy = extract_resume(b'Experience\nMaintain Linux servers.', 'txt', 'resume')['facts'][0]
        self.assertEqual(result['facts'][0]['id'], legacy['id'])

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

    def test_work_history_dates_and_email_responsibility_are_preserved(self):
        result = extract_resume(
            b'Example Applicant\nWork History\nExample Co | Engineer | 2020-2024\n'
            b'Maintained support@example.invalid routing and reduced incidents.\n'
            b'Technology Experience\nLinux administration\n', 'txt', 'uploaded-resume:one')
        self.assertIsNone(result['contact']['email'])
        self.assertIn(('experience', 'Example Co | Engineer | 2020-2024'),
                      [(f['kind'], f['text']) for f in result['facts']])
        self.assertIn(('experience', 'Maintained support@example.invalid routing and reduced incidents.'),
                      [(f['kind'], f['text']) for f in result['facts']])
        self.assertIn(('technology_experience', 'Linux administration'),
                      [(f['kind'], f['text']) for f in result['facts']])
        self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in result['facts']))

    def test_contact_links_are_reviewable_and_body_links_remain_facts(self):
        result = extract_resume(
            b'Example Applicant\napplicant@example.invalid | +1 202 555 0142\n'
            b'GitHub: github.com/example\nPortfolio: https://example.invalid/work\n'
            b'Projects\nBuilt https://github.com/employer/product for support@example.invalid\n',
            'txt', 'uploaded-resume:one')
        self.assertEqual(result['contact']['github'], 'https://github.com/example')
        self.assertEqual(result['contact']['portfolio'], 'https://example.invalid/work')
        self.assertEqual(result['contact']['email'], 'applicant@example.invalid')
        self.assertEqual(result['contact']['phones'], ['+1 202 555 0142'])
        self.assertEqual([(f['kind'], f['text']) for f in result['facts']],
                         [('project', 'Built https://github.com/employer/product for support@example.invalid')])

    def test_unlabelled_body_contact_is_not_attributed_to_applicant(self):
        result = extract_resume(b'Example Applicant\nExperience\nreference@example.invalid\n'
                                b'https://linkedin.com/in/supervisor\n', 'txt', 'resume')
        self.assertIsNone(result['contact']['email'])
        self.assertIsNone(result['contact']['linkedin'])
        self.assertEqual(len(result['facts']), 2)

    def test_unheaded_prose_ends_inferred_contact_header(self):
        result = extract_resume(b'Example Applicant\nManaged client systems.\n'
                                b'supervisor@example.invalid\nhttps://github.com/supervisor\n',
                                'txt', 'resume')
        self.assertIsNone(result['contact']['email'])
        self.assertIsNone(result['contact']['github'])
        self.assertEqual([f['text'] for f in result['facts']],
                         ['Managed client systems.', 'supervisor@example.invalid', 'https://github.com/supervisor'])

    def test_unsafe_contact_links_are_never_suggested(self):
        for link in ('javascript:alert(1)', 'https://user:password@example.invalid',
                     'https://example.invalid/with space'):
            result = extract_resume(('Example Applicant\nPortfolio: '+link+'\nSkills\nPython').encode(), 'txt', 'resume')
            self.assertIsNone(result['contact']['portfolio'])

    def test_native_pdf_and_docx_preserve_reviewable_chronology_and_links(self):
        from reportlab.pdfgen.canvas import Canvas
        from xml.sax.saxutils import escape
        lines = ['Example Applicant', 'GitHub: github.com/example',
                 'Work History', 'Example Co | Engineer | 2020-2024',
                 'Responsibilities', 'Maintained support@example.invalid routing.',
                 'Achievements', 'Reduced incidents by 30%.',
                 'Technology Experience', 'Linux administration']
        pdf = io.BytesIO()
        canvas = Canvas(pdf)
        text = canvas.beginText(40, 780)
        for line in lines:
            text.textLine(line)
        canvas.drawText(text)
        canvas.save()
        docx = io.BytesIO()
        with ZipFile(docx, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml',
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
                ''.join('<w:p><w:r><w:t>'+escape(line)+'</w:t></w:r></w:p>' for line in lines) +
                '</w:body></w:document>')
        for format, data in [('pdf', pdf.getvalue()), ('docx', docx.getvalue())]:
            with self.subTest(format=format):
                result = extract_resume(data, format, 'uploaded-resume:two')
                self.assertIsNone(result['contact']['email'])
                self.assertEqual(result['contact']['github'], 'https://github.com/example')
                self.assertEqual([(f['kind'],f['text']) for f in result['facts']], [
                    ('experience', 'Example Co | Engineer | 2020-2024'),
                    ('responsibility', 'Maintained support@example.invalid routing.'),
                    ('accomplishment', 'Reduced incidents by 30%.'),
                    ('technology_experience', 'Linux administration')])
                self.assertTrue(all(f['source'] == 'uploaded-resume:two' and not f['verified']
                                    and not f['user_approved'] for f in result['facts']))
                self.assertEqual(result['date_ranges'], [{'fact_id': result['facts'][0]['id'],
                    'raw': '2020-2024', 'start': '2020', 'end': '2024'}])


if __name__ == "__main__":
    unittest.main()
