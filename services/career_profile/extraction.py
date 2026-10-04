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
    "professional experience": "experience", "responsibilities": "experience",
    "technology experience": "technology_experience",
    "skills": "skill", "technical skills": "skill", "core skills": "skill",
    "education": "education", "certifications": "certification", "certification": "certification",
    "projects": "project", "accomplishments": "accomplishment", "achievements": "accomplishment",
}


def _empty_contact() -> dict:
    return {"name": None, "email": None, "phones": [], "linkedin": None, "city": None,
            "state": None, "postal_code": None, "country": None, "address_line1": None,
            "github": None, "portfolio": None}


def _native_pdf(data: bytes) -> tuple[str, list[str]]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return "", ["PDF is encrypted and requires a password"]
            except Exception:
                return "", ["PDF is encrypted and requires a password"]
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("Resume exceeds the supported page limit")
        chunks = []
        total = 0
        warnings = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
                remaining = MAX_TEXT_CHARS - total
                if remaining <= 0:
                    warnings.append("PDF text exceeded the supported extraction limit")
                    break
                if len(page_text) > remaining:
                    page_text = page_text[:remaining]
                    warnings.append("PDF text exceeded the supported extraction limit")
                chunks.append(page_text)
                total += len(page_text)
            except Exception:
                chunks.append("")
                warnings.append("Some PDF page text could not be extracted")
        return "\n".join(chunks), warnings
    except ValueError:
        raise
    except Exception:
        return "", ["PDF could not be read"]


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
        elif current in {"summary", "experience", "education", "certification", "project", "accomplishment", "technology_experience"}:
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
    try:
        if kind == "txt":
            text = bytes(data).decode("utf-8")
            warnings: list[str] = []
        elif kind == "pdf":
            text, warnings = _native_pdf(bytes(data))
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
        identifier = "fact_" + sha256((source + "\0" + fact_kind + "\0" + fact_text).encode("utf-8")).hexdigest()[:24]
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        facts.append({"id": identifier, "kind": fact_kind, "text": fact_text, "source": source,
                      "confidence": 90.0 if fact_kind == "skill" else 85.0,
                      "verified": False, "user_approved": False})
    status = "READY_FOR_REVIEW" if text.strip() else ("NEEDS_OCR" if kind == "pdf" and not warnings else "UNREADABLE")
    if not text.strip() and kind == "pdf" and not warnings:
        warnings = ["PDF contains no extractable text; OCR is required"]
    elif not text.strip() and kind != "pdf" and not warnings:
        warnings = ["Resume contains no readable text"]
    if overflow:
        warnings = list(warnings) + ["Some extracted facts exceeded review limits and were omitted"]
    return {"status": status, "warnings": warnings, "contact": contact, "facts": facts}
