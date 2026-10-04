"""Embedded Unicode PDF text, with explicit glyph and readback gates."""
from html import escape
import os
from pathlib import Path
from threading import RLock
import unicodedata

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from services.documents.errors import PreparationError

# ReportLab TTFont objects may not be used by concurrent documents. Serialize
# registration and complete rendering; the process registry has one owned font.
_FONT_LOCK = RLock()
_FONT_NAME = 'MJobsApprovedText'


def _font_path():
    candidates = [Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    if os.name == 'nt':
        candidates.insert(0, Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/'arial.ttf')
    for path in candidates:
        if path.is_file():
            return path
    raise PreparationError('DOCUMENT_FONT_UNAVAILABLE', 'Install the supported local font before generating PDF documents')


def render_pdf(path, paragraphs):
    with _FONT_LOCK:
        if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_font_path())))
        font = pdfmetrics.getFont(_FONT_NAME)
        characters = set(''.join(paragraphs))
        if any(not char.isspace() and (not font.face.charToGlyph.get(ord(char)) or
               unicodedata.bidirectional(char) in ('R','AL','AN','RLE','RLO','RLI','LRE','LRO','LRI','FSI','PDI','PDF'))
               for char in characters):
            raise PreparationError('DOCUMENT_UNSUPPORTED_GLYPH', 'The local PDF renderer cannot faithfully render this text; retain the original and review its characters')
        styles = getSampleStyleSheet()
        styles['Normal'].fontName = _FONT_NAME
        flowables = []
        for text in paragraphs:
            flowables.extend([Paragraph(escape(text), styles['Normal']), Spacer(1, 8)])
        SimpleDocTemplate(str(path)).build(flowables)
