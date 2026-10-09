"""Lightweight, deterministic acceptance card derived from progress_dashboard.snapshot.

Only the MJobs integration owner should call write_card at accepted integration
checkpoints. No filesystem watcher, CI workflow, or periodic updater is used.
"""
from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
import tempfile

CARD_JSON = Path("docs/PROGRESS_CARD.json")
CARD_SVG = Path("docs/PROGRESS_CARD.svg")
CARD_FILES = (CARD_JSON, CARD_SVG)


def _normalize(state: dict) -> dict:
    """Normalize validated canonical snapshot. No inference from Git history."""
    def counts(source: str, total_field: str, stamp_field: str) -> dict:
        counter = state[source]
        total = state[total_field]
        if not isinstance(total, int) or total <= 0:
            raise ValueError("Requirement total must be a positive integer")
        parts = {"pass": int(counter.get("PASS", 0)), "partial": int(counter.get("PARTIAL", 0)),
                 "not_run": int(counter.get("NOT_RUN", 0)), "total": total}
        if any(v < 0 for v in parts.values()) or sum(parts[k] for k in ("pass", "partial", "not_run")) != total:
            raise ValueError(f"Inconsistent {source} requirement counts")
        parts["accepted_percent"] = round(parts["pass"] * 100 / total, 1)
        parts["as_of"] = str(state[stamp_field])
        return parts
    return {
        "schema_version": 1,
        "source": "MJobs canonical acceptance matrices; verified checkpoints, not real-time coding activity",
        "original": counts("original", "original_total", "original_as_of"),
        "reference": counts("reference", "reference_total", "reference_as_of"),
        "milestone": str(state["milestone"]),
        "release_readiness": str(state["release"]),
    }


def render_json(state: dict) -> str:
    return json.dumps(_normalize(state), indent=2, ensure_ascii=False) + "\n"


def render_svg(state: dict) -> str:
    s = _normalize(state)
    c = s["original"]
    accepted_width = round(838 * c["pass"] / c["total"], 2)
    as_of = escape(c["as_of"])
    alt = escape(f"MJobs accepted {c['pass']} of {c['total']} requirements; {c['partial']} partial, "
                 f"{c['not_run']} not run, {c['accepted_percent']:.1f}% accepted; matrix as of {c['as_of']}")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{alt}" viewBox="0 0 900 176" width="900" height="176">
  <title>MJobs verified requirement acceptance</title>
  <rect x="0" y="0" width="900" height="176" rx="12" fill="#101113"/>
  <g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif">
    <text x="30" y="35" font-size="16" font-weight="600" letter-spacing="0.5" fill="#AFB7C4">MJOBS — VERIFIED REQUIREMENT ACCEPTANCE</text>
    <text x="30" y="77" font-size="31" font-weight="700" fill="#F7FAFD">{c['pass']} / {c['total']}</text>
    <text x="205" y="76" font-size="17" fill="#D0D5DD">original requirements fully accepted</text>
    <rect x="30" y="98" width="838" height="12" rx="6" fill="#303238"/>
    <rect x="30" y="98" width="{accepted_width}" height="12" rx="6" fill="#27B56E"/>
    <text x="30" y="139" font-size="15" fill="#CBD0D9">{c['partial']} partial · {c['not_run']} not yet validated</text>
    <text x="868" y="139" text-anchor="end" font-size="16" font-weight="700" fill="#F3F5F8">{c['accepted_percent']:.1f}%</text>
    <text x="30" y="162" font-size="11" fill="#939AA6">Canonical matrix checkpoint: {as_of} · no inference from unmerged work</text>
  </g>
</svg>\n'''


def expected_files(state: dict) -> dict[Path, str]:
    return {CARD_JSON: render_json(state), CARD_SVG: render_svg(state)}


def card_current(root: Path, state: dict) -> bool:
    root = Path(root)
    return all((root / path).is_file() and (root / path).read_text(encoding="utf-8") == text
               for path, text in expected_files(state).items())


def _atomic_write(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix=".progress-card-", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def write_card(root: Path, state: dict) -> bool:
    """Idempotently write the two small card files; integration owner only."""
    root = Path(root)
    return any([_atomic_write(root / path, data) for path, data in expected_files(state).items()])
