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
_BOLD_FONT_NAME = 'MJobsApprovedBoldText'


def _font_path(bold=False):
    candidates = [Path('/usr/share/fonts/truetype/dejavu')/('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')]
    if os.name == 'nt':
        candidates.insert(0, Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/('arialbd.ttf' if bold else 'arial.ttf'))
    for path in candidates:
        if path.is_file():
            return path
    raise PreparationError('DOCUMENT_FONT_UNAVAILABLE', 'Install the supported local font before generating PDF documents')


def render_pdf(path, paragraphs, heading_indices=(), bold_indices=()):
    with _FONT_LOCK:
        if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_font_path())))
        if bold_indices and _BOLD_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_BOLD_FONT_NAME, str(_font_path(bold=True))))
        for index,value in enumerate(paragraphs):
            font=pdfmetrics.getFont(_BOLD_FONT_NAME if index in bold_indices else _FONT_NAME)
            if any(not char.isspace() and (not font.face.charToGlyph.get(ord(char)) or
                   unicodedata.bidirectional(char) in ('R','AL','AN','RLE','RLO','RLI','LRE','LRO','LRI','FSI','PDI','PDF'))
                   for char in value):
                raise PreparationError('DOCUMENT_UNSUPPORTED_GLYPH', 'The local PDF renderer cannot faithfully render this text; retain the original and review its characters')
        styles = getSampleStyleSheet()
        styles['Normal'].fontName = _FONT_NAME
        styles['Heading2'].fontName = _FONT_NAME
        styles['Heading2'].fontSize = 13
        styles['Heading2'].leading = 16
        bold_style=styles['Normal'].clone('ApprovedBold')
        bold_style.fontName=_BOLD_FONT_NAME
        flowables = []
        for index,text in enumerate(paragraphs):
            style=bold_style if index in bold_indices else styles['Heading2' if index in heading_indices else 'Normal']
            flowables.extend([Paragraph(escape(text),style), Spacer(1, 8)])
        SimpleDocTemplate(str(path)).build(flowables)
