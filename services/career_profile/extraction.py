"""Bounded, deterministic native extraction for user supplied resumes."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import re
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET


MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 300_000
MAX_PDF_PAGES = 100
MAX_DOCX_MEMBERS = 1_000
MAX_DOCX_UNCOMPRESSED = 30 * 10 * 1024 * 1024
MAX_DOCX_XML_BYTES = 2 * 1024 * 1024

_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_PHONE = re.compile(r"(?<!\w)\+?[\d(). -]{10,24}(?!\w)")
_LINKEDIN = re.compile(r"(?i)(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9][A-Za-z0-9_-]*")
_GITHUB = re.compile(r"(?i)(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_HEADINGS = {
    "summary": "summary", "professional summary": "summary", "profile": "summary",
    "experience": "experience", "work experience": "experience", "employment": "experience",
    "work history": "experience", "employment history": "experience",
    "professional experience": "experience", "responsibilities": "responsibility",
    "technology experience": "technology_experience",
    "skills": "skill", "technical skills": "skill", "core skills": "skill",
    "education": "education", "certifications": "certification", "certification": "certification",
    "projects": "project", "accomplishments": "accomplishment", "achievements": "accomplishment",
}

_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july',
           'august', 'september', 'october', 'november', 'december')
_MONTH_TOKEN = '(?:' + '|'.join(month[:3] + '(?:' + month[3:] + ')?' if len(month) > 3 else month
                              for month in _MONTHS) + r')\.?'
_YEAR_TOKEN = r'(?:19|20)\d{2}'
_DATE_TOKEN = (rf'(?:{_YEAR_TOKEN}-(?:0[1-9]|1[0-2])|(?:0?[1-9]|1[0-2])/{_YEAR_TOKEN}|'
               rf'{_MONTH_TOKEN}\s+{_YEAR_TOKEN}|{_YEAR_TOKEN})')
_DATE_RANGE = re.compile(rf'(?<![\w/.–—-])(?P<start>{_DATE_TOKEN})\s*(?:[-–—]|\bto\b)\s*'
                         rf'(?P<end>{_DATE_TOKEN}|present|current)(?![\w/–—-]|\.\w)', re.IGNORECASE)


def _review_date_range(text: str) -> dict | None:
    """Project one unambiguous literal range; never infer tenure or missing dates."""
    matches = list(_DATE_RANGE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    # A spaced chain can otherwise look like one valid pair plus a stray date.
    if re.match(rf'\s*(?:[-–—,;]|\bto\b)\s*(?:{_DATE_TOKEN}|present|current)',
                text[match.end():], re.IGNORECASE):
        return None
    if re.search(rf'{_DATE_TOKEN}\s*[,;]\s*$', text[:match.start()], re.IGNORECASE):
        return None

    def normalize(value):
        value = value.lower().rstrip('.')
        if value in {'present', 'current'}:
            return 'PRESENT'
        if re.fullmatch(_YEAR_TOKEN, value):
            return value
        if re.fullmatch(r'\d{4}-\d{2}', value):
            return value
        if '/' in value:
            month, year = value.split('/')
        else:
            month_name, year = value.split()
            month = next(index for index, name in enumerate(_MONTHS, 1) if name.startswith(month_name.rstrip('.')))
        return f'{year}-{int(month):02d}'

    start, end = normalize(match['start']), normalize(match['end'])
    if end != 'PRESENT' and (start[:4] > end[:4] or
                            len(start) == len(end) == 7 and start > end):
        return None
    return {'raw': match[0], 'start': start, 'end': end}


def _empty_contact() -> dict:
    return {"name": None, "email": None, "phones": [], "linkedin": None, "city": None,
            "state": None, "postal_code": None, "country": None, "address_line1": None,
            "github": None, "portfolio": None}


def _page_has_images(page):
    """Inspect bounded resource metadata without decoding embedded image pixels."""
    pending, visited = [(page, 0)], set()
    while pending and len(visited) < 200:
        obj, depth = pending.pop()
        resources = obj.get('/Resources', {}).get_object() if hasattr(obj.get('/Resources', {}), 'get_object') else obj.get('/Resources', {})
        xobjects = resources.get('/XObject', {})
        xobjects = xobjects.get_object() if hasattr(xobjects, 'get_object') else xobjects
        for reference in list(xobjects.values())[:200]:
            if len(visited) >= 200:
                break
            value = reference.get_object()
            if id(value) in visited:
                continue
            visited.add(id(value))
            if value.get('/Subtype') == '/Image':
                return True
            if value.get('/Subtype') == '/Form' and depth < 4:
                pending.append((value, depth+1))
    return False


def _native_pdf(data: bytes) -> tuple[str, list[str], bool, bool]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return "", ["PDF is encrypted and requires a password"], False, False
            except Exception:
                return "", ["PDF is encrypted and requires a password"], False, False
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("Resume exceeds the supported page limit")
        chunks = []
        total = 0
        warnings = []
        scanned = []
        for number, page in enumerate(reader.pages, 1):
            try:
                page_text = page.extract_text() or ""
                remaining = MAX_TEXT_CHARS - total
                if remaining <= 0:
                    warnings.append("PDF text exceeded the supported extraction limit")
                    break
                if len(page_text) > remaining:
                    page_text = page_text[:remaining]
                    warnings.append("PDF text exceeded the supported extraction limit")
                is_scanned = not page_text.strip() and _page_has_images(page)
                chunks.append(page_text)
                total += len(page_text)
                if is_scanned:
                    scanned.append(number)
            except Exception:
                chunks.append("")
                warnings.append("Some PDF page text could not be extracted")
        from services.career_profile.ocr import ocr_pdf_pages
        try:
            recognized, ocr_warnings = ocr_pdf_pages(data, scanned)
        except Exception:
            # OCR is optional enrichment. A helper failure must never erase
            # native text already recovered from the same document.
            recognized, ocr_warnings = {}, ['Local OCR failed. Native text remains available for review.']
        warnings.extend(ocr_warnings)
        for number, value in recognized.items():
            chunks[number-1] = value
        if recognized:
            warnings.append('OCR text may contain mistakes. Verify all extracted facts and contact details before use.')
        combined = '\n'.join(chunks)
        if len(combined) > MAX_TEXT_CHARS:
            combined = combined[:MAX_TEXT_CHARS]
            warnings.append('PDF text exceeded the supported extraction limit')
        return combined, warnings, bool(scanned), bool(recognized)
    except ValueError:
        raise
    except Exception:
        return "", ["PDF could not be read"], False, False


def _native_docx(data: bytes) -> tuple[str, list[str]]:
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            names = {item.filename for item in members}
            if len(members) > MAX_DOCX_MEMBERS or sum(item.file_size for item in members) > MAX_DOCX_UNCOMPRESSED:
                raise ValueError("Resume archive exceeds the supported resource limit")
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                return "", ["DOCX document parts are missing"]
            if any(item.flag_bits & 1 or item.filename.startswith("/") or "\\" in item.filename or
                   any(part in (".", "..") for part in item.filename.split("/")) for item in members):
                return "", ["DOCX archive contains unsafe entries"]
            if archive.getinfo('word/document.xml').file_size > MAX_DOCX_XML_BYTES:
                raise ValueError('Resume XML part exceeds the supported resource limit')
            with archive.open('word/document.xml') as part:
                xml = part.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml) > MAX_DOCX_XML_BYTES:
                raise ValueError('Resume XML part exceeds the supported resource limit')
            root = ET.fromstring(xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = []
            for paragraph in root.findall(".//w:p", ns):
                value = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                if value:
                    paragraphs.append(value)
            return "\n".join(paragraphs), []
    except ValueError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, OSError):
        return "", ["DOCX could not be read"]


def _section_facts(text: str, contact_lines: set[int]) -> list[tuple[str, str]]:
    current = "summary"
    facts: list[tuple[str, str]] = []
    for index, raw in enumerate(line for line in text.splitlines() if line.strip()):
        line = raw.strip()
        if not line:
            continue
        heading = re.sub(r"[:\s]+$", "", line).lower()
        if heading in _HEADINGS:
            current = _HEADINGS[heading]
            continue
        if index in contact_lines:
            continue
        if current == "skill":
            # Split only conventional delimiters; preserve each user supplied token.
            parts = re.split(r"\s*(?:[,;|•])\s*", line)
            facts.extend(("skill", part.strip(" -\t")) for part in parts if part.strip(" -\t"))
        elif current in {"summary", "experience", "responsibility", "education", "certification", "project", "accomplishment", "technology_experience"}:
            facts.append((current, line))
    return facts


def _contact_token(token: str, *, in_header: bool) -> tuple[str, str] | None:
    """Recognize whole contact tokens, never an address/link inside career prose."""
    label = re.fullmatch(r"(?i)(email|e-mail|phone|tel|telephone|mobile|cell|linkedin|github|portfolio|website)\s*:\s*(.+)", token)
    key, value = (label[1].lower(), label[2].strip()) if label else (None, token)
    if not in_header and not label:
        return None
    if key in (None, 'email', 'e-mail') and _EMAIL.fullmatch(value):
        return 'email', value
    if key in (None, 'phone', 'tel', 'telephone', 'mobile', 'cell') and _PHONE.fullmatch(value):
        if 10 <= len(re.sub(r'\D', '', value)) <= 15:
            return 'phones', value
    for field, pattern in (('linkedin', _LINKEDIN), ('github', _GITHUB)):
        if key in (None, field) and pattern.fullmatch(value.rstrip('/')):
            return field, value if value.lower().startswith('http') else 'https://' + value
    if key in ('portfolio', 'website') and len(value) <= 1000:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                    and not parsed.password and not any(c.isspace() for c in value)):
                return 'portfolio', value
        except ValueError:
            pass
    return None


def _extract_contact(text: str) -> tuple[dict, set[int]]:
    contact = _empty_contact()
    used: set[int] = set()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headings = set(_HEADINGS)
    if lines and lines[0].lower().rstrip(":") not in headings and re.fullmatch(
            r"[A-Za-z][A-Za-z .'-]+", lines[0]) and 2 <= len(lines[0].split()) <= 5 and len(lines[0]) <= 100:
        contact["name"] = lines[0]
        used.add(0)
    heading_positions = [i for i, line in enumerate(lines) if line.lower().rstrip(':').strip() in headings]
    header_end = min(heading_positions) if heading_positions else len(lines)
    for index, line in enumerate(lines[:header_end]):
        line = line.strip()
        explicit_location = re.match(r'(?i)^(?:location|address)\s*:\s*', line)
        if explicit_location:
            line = line[explicit_location.end():]
        elif not heading_positions:
            # Without section boundaries an employer line is indistinguishable
            # from a home location. Leave it for explicit manual entry.
            continue
        location = re.match(r"^([^,\n]+),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$", line)
        if location:
            contact["city"], contact["state"], contact["postal_code"] = location.groups()
            used.add(index)
            break
    phones = []
    for index, line in enumerate(lines):
        tokens = [part.strip() for part in re.split(r'[|•]', line) if part.strip()]
        candidates = [_contact_token(token, in_header=index < header_end) for token in tokens]
        if not heading_positions and index not in used and not all(candidates):
            # Without section markers, only the opening contact block supports
            # unlabelled suggestions. Career prose ends that inferred header.
            header_end = min(header_end, index)
            candidates = [_contact_token(token, in_header=False) for token in tokens]
        for candidate in candidates:
            if candidate is None:
                continue
            key, value = candidate
            if key == 'phones':
                phones.append(value)
            elif contact[key] is None:
                contact[key] = value
        if candidates and all(candidates):
            used.add(index)
    contact["phones"] = list(dict.fromkeys(phones))[:5]
    return contact, used


def extract_resume(data: bytes, format: str, source: str) -> dict:
    """Extract reviewable facts from PDF, DOCX, or UTF-8 TXT bytes."""
    if not isinstance(source, str) or len(source) > 1000:
        raise ValueError("Resume source is too long")
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_INPUT_BYTES:
        raise ValueError("Resume exceeds the supported size limit")
    kind = str(format).lower().lstrip(".")
    needs_ocr = used_ocr = False
    try:
        if kind == "txt":
            text = bytes(data).decode("utf-8")
            warnings: list[str] = []
        elif kind == "pdf":
            text, warnings, needs_ocr, used_ocr = _native_pdf(bytes(data))
        elif kind == "docx":
            text, warnings = _native_docx(bytes(data))
        else:
            return {"status": "UNREADABLE", "warnings": ["Unsupported resume format"], "contact": _empty_contact(), "facts": []}
    except UnicodeDecodeError:
        return {"status": "UNREADABLE", "warnings": ["Resume text encoding could not be read"], "contact": _empty_contact(), "facts": []}
    except ValueError:
        raise
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("Resume text exceeds the supported size limit")
    contact, contact_lines = _extract_contact(text)
    pairs = _section_facts(text, contact_lines)
    facts = []
    seen_ids = set()
    overflow = False
    for fact_kind, fact_text in pairs:
        if len(facts) >= 200:
            overflow = True
            continue
        if len(fact_text) > 10000:
            overflow = True
            continue
        # Responsibilities used to be classified as experience. Retain their
        # established IDs so re-extraction cannot duplicate already-saved facts.
        identity_kind = 'experience' if fact_kind == 'responsibility' else fact_kind
        identifier = "fact_" + sha256((source + "\0" + identity_kind + "\0" + fact_text).encode("utf-8")).hexdigest()[:24]
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        facts.append({"id": identifier, "kind": fact_kind, "text": fact_text, "source": source,
                      "confidence": 70.0 if used_ocr else (90.0 if fact_kind == "skill" else 85.0),
                      "verified": False, "user_approved": False})
    status = "READY_FOR_REVIEW" if text.strip() else ("NEEDS_OCR" if needs_ocr else "UNREADABLE")
    if not text.strip() and not warnings:
        warnings = ["Resume contains no readable text"]
    if overflow:
        warnings = list(warnings) + ["Some extracted facts exceeded review limits and were omitted"]
    date_ranges = []
    for fact in facts:
        if fact['kind'] in {'experience', 'education', 'project', 'technology_experience'}:
            period = _review_date_range(fact['text'])
            if period:
                date_ranges.append({'fact_id': fact['id'], **period})
    return {"status": status, "warnings": warnings, "contact": contact, "facts": facts,
            "date_ranges": date_ranges}
