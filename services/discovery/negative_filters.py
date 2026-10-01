"""Explicit job-condition detection for user-configured negative filters."""
from __future__ import annotations

import re

NEGATIVE_CONDITIONS = {
    "ON_CALL",
    "NIGHT_SHIFT",
    "CONTRACT_1099",
    "COMMISSION",
    "DOOR_TO_DOOR",
    "CLEARANCE_REQUIRED",
    "MANDATORY_RELOCATION",
}

PATTERNS = (
    ("ON_CALL", r"\bon[- ]call\b"),
    ("NIGHT_SHIFT", r"\b(?:night|overnight|graveyard)\s+shift\b|\bthird\s+shift\b"),
    ("CONTRACT_1099", r"\b1099\b"),
    ("COMMISSION", r"\bcommission(?:[- ]based|[- ]only)?\b"),
    ("DOOR_TO_DOOR", r"\bdoor[- ]to[- ]door\b"),
    ("MANDATORY_RELOCATION", r"\b(?:relocation\s+(?:is\s+)?required|must\s+relocate|required\s+to\s+relocate)\b"),
)

def _clean(value):
    return " ".join(str(value or "").split())

def _sentence(text, start, end):
    value = str(text or "")
    left = max(value.rfind(".", 0, start), value.rfind("\n", 0, start), value.rfind(";", 0, start))
    right_candidates = [index for index in (value.find(".", end), value.find("\n", end), value.find(";", end)) if index >= 0]
    right = min(right_candidates) if right_candidates else len(value)
    return _clean(value[left + 1:right])

def _travel_percent(text):
    value = str(text or "")
    patterns = (
        r"\btravel(?:ing)?\s+(?:up\s+to\s+|approximately\s+|about\s+)?(?P<pct>\d{1,3})\s*%",
        r"\b(?P<pct>\d{1,3})\s*%\s+(?:domestic\s+|international\s+)?travel\b",
        r"\btravel(?:ing)?\s+(?:may\s+be\s+|is\s+)?(?:greater\s+than|more\s+than|over)\s+(?P<pct>\d{1,3})\s*%",
    )
    rows = []
    for pattern in patterns:
        for match in re.finditer(pattern, value, re.I):
            percent = int(match.group("pct"))
            if 0 <= percent <= 100:
                rows.append({
                    "percent": percent,
                    "raw": _clean(match.group(0)),
                    "clause": _sentence(value, match.start(), match.end()),
                })
    return rows
def parse_negative_conditions(text, clearance=None):
    value = str(text or "")
    flags = []
    evidence = []
    for flag, pattern in PATTERNS:
        match = re.search(pattern, value, re.I)
        if match:
            flags.append(flag)
            evidence.append({
                "condition": flag,
                "raw": _clean(match.group(0)),
                "clause": _sentence(value, match.start(), match.end()),
            })

    clearance_level = None
    if isinstance(clearance, dict):
        clearance_level = clearance.get("level")
    if clearance_level and clearance_level != "NONE_STATED":
        flags.append("CLEARANCE_REQUIRED")
        evidence.append({
            "condition": "CLEARANCE_REQUIRED",
            "raw": clearance.get("raw"),
            "clause": clearance.get("clause"),
        })

    travel = _travel_percent(value)
    travel_percent = max((row["percent"] for row in travel), default=None)
    evidence.extend({
        "condition": "TRAVEL_PERCENT",
        "raw": row["raw"],
        "clause": row["clause"],
        "percent": row["percent"],
    } for row in travel)

    return {
        "flags": sorted(set(flags)),
        "travel_percent": travel_percent,
        "evidence": evidence,
    }
