"""Readable, deterministic console presentation of Codex exec logs.

Underlying logs stay byte-for-byte intact. This display layer never claims
to be the interactive Codex TUI or guesses which AI turn wrote a Git commit.
"""
from __future__ import annotations

import re

START_MARKER = "===== START "
EXIT_MARKER = "=== CLI EXIT "


def latest_run_excerpt(raw: str, *, max_chars: int = 16000) -> str:
    """Return a coherent last-run *summary*, never a mid-line diff fragment."""
    ix = raw.rfind(START_MARKER)
    current = raw[ix:] if ix >= 0 else raw
    if not current.strip():
        return "No completed run has been recorded yet."
    end = current.rfind(EXIT_MARKER)
    completed = end >= 0
    if completed:
        footer = current[end:].splitlines()[0]
        body = current[:end]
    else:
        footer = "CLI still running or finished without an exit marker"
        body = current
    # Codex prints a distinct final-response segment after token counts.
    # Windows console logs commonly use CRLF. A fixed "\\ntokens used\\n"
    # search misses that separator and falls back to raw diff fragments.
    markers = list(re.finditer(r"(?m)^[ \t]*tokens used[ \t]*\r?$", body))
    if markers:
        response = body[markers[-1].end():]
        lines = [line for line in response.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and re.fullmatch(r"[0-9,]+", lines[0].strip()):
            lines = lines[1:]
        summary = "\n".join(lines).strip()
        if summary:
            return ("LATEST CODEX RUN SUMMARY (full raw transcript remains on disk)\n"
                    + "-" * 65 + "\n"
                    + summary[-max_chars:] + "\n\n" + footer)
    # During execution show only bounded whole lines, not arbitrary raw bytes.
    recent = []
    suppress = False
    for line in body.splitlines():
        if line.startswith(("diff --git ", "+++ b/", "--- a/")):
            if not suppress:
                recent.append("[Source diff omitted from viewer; see dashboard raw log]")
            suppress = True
            continue
        if suppress and (line.strip() in {"exec", "codex", "tokens used"}
                         or line.startswith(("=== ", "===== ", "**Status:"))):
            suppress = False
        if suppress or line.startswith(("@@ ", "+", "-")):
            continue
        if line.strip():
            recent.append(line)
    trimmed = "\n".join(recent[-32:]).strip()
    return ("RECENT CODING ACTIVITY (raw code diffs hidden in this viewer)\n"
            + "-" * 65 + "\n"
            + (trimmed or "Codex has not emitted readable progress yet.")
            + "\n\n" + footer)


class LiveDisplay:
    """Incrementally suppress raw Git patch hunks while preserving summary text."""

    def __init__(self):
        self._inside_diff = False
        self._diff_announced = False
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        # Never print incomplete partial lines.
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        shown = []
        for raw in lines:
            line = raw.rstrip("\r")
            if line.startswith("diff --git "):
                self._inside_diff = True
                if not self._diff_announced:
                    shown.append("[Code patch generated; complete raw diff is in the dashboard log]")
                self._diff_announced = True
                continue
            if self._inside_diff:
                if line.strip() in {"exec", "codex", "tokens used"} or line.startswith(
                    ("=== ", "===== ", "**Status:")
                ):
                    self._inside_diff = False
                    self._diff_announced = False
                else:
                    continue
            if line.startswith(("@@ ", "+++ b/", "--- a/")):
                continue
            # Do not flood the visible viewer with thousands of individual
            # source-code lines, but still show all high-level Codex activity.
            if line.startswith(("+", "-")):
                continue
            shown.append(line)
        return "\n".join(shown) + ("\n" if shown else "")
