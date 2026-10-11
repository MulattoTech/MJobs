"""Read bounded, checkout-local pip includes without network or package execution."""
from __future__ import annotations

from pathlib import Path
import re

INCLUDE = re.compile(r'^(?:-r\s*|--requirement(?:=|\s+))(.+)$')
MAX_FILES = 16
MAX_BYTES = 131072


def requirement_files(root: Path) -> dict[str, str]:
    """Return the legacy entry and its includes, rejecting missing/escaping inputs."""
    root = Path(root).resolve()
    entry = root / 'requirements.lock'
    if not entry.is_file():
        entry = root / 'requirements.txt'
    if not entry.is_file():
        return {}
    files: dict[str, str] = {}
    active: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError('Requirement include escapes checkout')
        if resolved in active:
            raise ValueError('Cyclic requirement include')
        name = resolved.relative_to(root).as_posix()
        if name in files:
            return
        if len(files) >= MAX_FILES or not resolved.is_file():
            raise ValueError('Missing requirement include or too many inputs')
        if resolved.stat().st_size > MAX_BYTES:
            raise ValueError('Requirement manifest exceeds size limit')
        try:
            text = resolved.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as error:
            raise ValueError('Unreadable requirement manifest') from error
        files[name] = text
        active.add(resolved)
        for line in text.splitlines():
            line = line.partition('#')[0].strip()
            match = INCLUDE.fullmatch(line)
            if match:
                target = match.group(1).strip().strip('\"\'')
                if not target or ':' in target or Path(target).is_absolute():
                    raise ValueError('Only local relative requirement includes are supported')
                visit(resolved.parent / target)
        active.remove(resolved)

    visit(entry)
    return files


def requirement_lines(root: Path) -> tuple[str, ...]:
    """Compare actual requirement declarations, not just identical -r wrappers."""
    rows = []
    for content in requirement_files(root).values():
        for line in content.splitlines():
            line = line.partition('#')[0].strip()
            if line and not INCLUDE.fullmatch(line):
                rows.append(line)
    return tuple(sorted(rows))
