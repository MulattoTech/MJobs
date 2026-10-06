"""Shell-free child entry point for deadline-bounded native resume parsing."""
from __future__ import annotations

import json
import sys

from services.career_profile.extraction import _native_docx, _native_pdf_pages


def _apply_linux_limits() -> None:
    if not sys.platform.startswith('linux'):
        return
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (805306368, 805306368))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16777216, 16777216))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {'pdf', 'docx'}:
        return 2
    data = sys.stdin.buffer.read()
    try:
        _apply_linux_limits()
        if sys.argv[1] == 'pdf':
            pages, warnings, scanned = _native_pdf_pages(data)
        else:
            text, warnings = _native_docx(data)
            pages, scanned = ([text] if text else []), []
        response = {'pages': pages, 'warnings': warnings, 'scanned': scanned}
    except ValueError as exc:
        message = str(exc)
        if message == 'Resume exceeds the supported page limit':
            response = {'error': 'page_limit'}
        elif message == 'Resume archive exceeds the supported resource limit':
            response = {'error': 'archive_limit'}
        elif message == 'Resume XML part exceeds the supported resource limit':
            response = {'error': 'xml_limit'}
        else:
            response = {'pages': [], 'warnings': ['Resume could not be read'], 'scanned': []}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
