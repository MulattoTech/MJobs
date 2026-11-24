"""Generate and validate the read-only repository ownership index."""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("docs/OWNERSHIP_INDEX.json")
SOURCE_DOCS = (Path("docs/REPO_MAP.md"), Path("docs/AREA_ROUTING.md"))
AREA_CONTRACT_GLOB = "**/AREA.md"
REQUIREMENT = re.compile(r"MJ-(?:REQ|REF)-\d{3}(?:\.[A-Z0-9]+)?")


def _read(root: Path, relative: Path) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table(text: str, expected_headers: tuple[str, ...]) -> list[dict[str, str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        headers = [part.strip().lower() for part in line.strip("|").split("|")]
        if headers[:len(expected_headers)] != list(expected_headers):
            continue
        if index + 1 >= len(lines) or not re.fullmatch(r"\|?[\s|:-]+\|?", lines[index + 1]):
            continue
        rows = []
        for row in lines[index + 2:]:
            if not row.startswith("|"):
                break
            values = [part.strip() for part in row.strip("|").split("|")]
            if len(values) != len(headers):
                continue
            rows.append(dict(zip(headers, values)))
        return rows
    raise ValueError(f"Could not find Markdown table with headers: {expected_headers}")


def _link_paths(value: str, source: Path) -> list[str]:
    links = re.findall(r"\[.*?\]\(([^)]+)\)", value)
    if not links:
        return [value.strip("`")]
    return [
        posixpath.normpath(posixpath.join(source.parent.as_posix(), link)).rstrip("/")
        + "/"
        for link in links
    ]


def _area_contract(root: Path, path: Path) -> dict[str, object]:
    text = _read(root, path)
    lines = text.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), path.parent.name)
    paragraphs = [part.strip().replace("\n", " ") for part in re.split(r"\n\s*\n", text)]
    summary = next((part for part in paragraphs if part and not part.startswith("#")), "")
    return {
        "path": path.as_posix(),
        "title": title,
        "summary": summary,
        "requirements": sorted(set(REQUIREMENT.findall(text))),
        "sha256": _hash(text),
    }


def build_index(root: Path = ROOT) -> dict[str, object]:
    root = Path(root).resolve()
    repo_map = _table(_read(root, SOURCE_DOCS[0]), ("path", "ownership"))
    area_routing = _table(
        _read(root, SOURCE_DOCS[1]),
        ("area", "requirement range", "intended code ownership", "area spec"),
    )
    contracts = [
        _area_contract(root, path.relative_to(root))
        for path in sorted(root.glob(AREA_CONTRACT_GLOB))
    ]
    return {
        "schema_version": 1,
        "sources": [
            {"path": path.as_posix(), "sha256": _hash(_read(root, path))}
            for path in SOURCE_DOCS
        ],
        "repository_map": [
            {
                "paths": _link_paths(row["path"], SOURCE_DOCS[0]),
                "ownership": row["ownership"].strip("`"),
            }
            for row in repo_map
        ],
        "area_routing": [
            {
                "area": row["area"].strip("`"),
                "requirements": row["requirement range"].strip("`"),
                "code_ownership": row["intended code ownership"].strip("`"),
                "area_spec": row["area spec"].strip("`"),
            }
            for row in area_routing
        ],
        "area_contracts": contracts,
    }


def render(index: dict[str, object]) -> str:
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def write_index(root: Path = ROOT) -> bool:
    root = Path(root).resolve()
    output = root / OUTPUT
    expected = render(build_index(root))
    changed = not output.is_file() or output.read_text(encoding="utf-8") != expected
    if not changed:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=".ownership-", suffix=".tmp",
        delete=False, newline="\n",
    ) as handle:
        temporary = Path(handle.name)
        handle.write(expected)
    try:
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def check_index(root: Path = ROOT) -> tuple[bool, str]:
    root = Path(root).resolve()
    output = root / OUTPUT
    if not output.is_file():
        return False, f"Ownership index missing: {OUTPUT.as_posix()}"
    try:
        current = output.read_text(encoding="utf-8")
        expected = render(build_index(root))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return False, f"Ownership index invalid: {error}"
    if current != expected:
        return False, f"Ownership index stale: {OUTPUT.as_posix()}"
    return True, f"Ownership index current: {OUTPUT.as_posix()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.check:
            current, message = check_index(args.root)
            print(message)
            return 0 if current else 1
        if args.write:
            print("Ownership index: UPDATED" if write_index(args.root) else "Ownership index: UNCHANGED")
            return 0
        print(render(build_index(args.root)), end="")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"ownership_index: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
