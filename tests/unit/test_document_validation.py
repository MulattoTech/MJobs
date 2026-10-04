"""Deterministic claim checks, format corruption and concurrent font use."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from docx import Document
from pypdf import PdfReader
from services.documents.errors import PreparationError
from services.documents.render import render_pdf
from services.documents.validation import validate_assertions, validate_formats


class DocumentValidationUnitTests(unittest.TestCase):
    def test_assertions_reject_unapproved_ids_added_prose_and_changed_identity(self):
        profile={'name':'Alex','email':'alex@example.test','facts':[
            {'fact_id':'skill','text':'Python','verified':True,'user_approved':True,'confidence':100,'source':'fixture'},
            {'fact_id':'pending','text':'SQL','verified':True,'user_approved':False,'confidence':100,'source':'fixture'}]}
        for paragraphs,ids in [(['Alex','alex@example.test','SQL'],['pending']),
                (['Alex','alex@example.test','Python expert'],['skill']),
                (['Other','alex@example.test','Python'],['skill']),
                (['Alex','alex@example.test','Python','Python'],['skill','skill'])]:
            with self.subTest(paragraphs=paragraphs),self.assertRaises(PreparationError) as error:
                validate_assertions('resume',paragraphs,profile,ids)
            self.assertEqual(error.exception.code,'UNSUPPORTED_CLAIM')

    def test_parallel_pdf_rendering_and_format_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            texts=['Δοκιμή Система résumé '+str(i) for i in range(8)]
            def render(i):
                path=root/f'{i}.pdf';render_pdf(path,[texts[i]])
                return PdfReader(path).pages[0].extract_text().strip()
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(render,range(8))),texts)
            paths={'pdf':root/'0.pdf','docx':root/'test.docx','txt':root/'test.txt'}
            doc=Document();doc.add_paragraph(texts[0]);doc.save(paths['docx'])
            paths['txt'].write_text(texts[0]+'\n',encoding='utf-8')
            validate_formats(paths,[texts[0]])
            for fmt in ('txt','pdf','docx'):
                original=paths[fmt].read_bytes()
                if fmt=='txt':paths[fmt].write_text('Changed\n',encoding='utf-8')
                elif fmt=='pdf':render_pdf(paths[fmt],['Changed'])
                else:
                    doc=Document();doc.add_paragraph('Changed');doc.save(paths[fmt])
                with self.subTest(format=fmt),self.assertRaises(PreparationError) as error:
                    validate_formats(paths,[texts[0]])
                self.assertEqual(error.exception.code,'DOCUMENT_CONTENT_MISMATCH')
                paths[fmt].write_bytes(original)
