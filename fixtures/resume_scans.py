"""Synthetic scanned resume pages for local OCR acceptance; no career claims."""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


def scanned_resume(*, mixed=False):
    image = Image.new('RGB', (1400, 1800), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    for i, line in enumerate(('Skills', 'Python, SQL', 'Experience', 'Engineer 2020 - 2024')):
        draw.text((80, 100+i*100), line, fill='black', font=font)
    output = BytesIO()
    pdf = canvas.Canvas(output)
    if mixed:
        pdf.drawString(40, 780, 'Example Applicant')
        pdf.drawString(40, 760, 'example@example.invalid')
        pdf.showPage()
    pdf.drawImage(ImageReader(image), 30, 50, width=535, height=688)
    pdf.save()
    return output.getvalue()
