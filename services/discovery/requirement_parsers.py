"""Conservative deterministic parsing for explicit job requirements."""
from __future__ import annotations

import re

from services.discovery.skill_taxonomy import SKILL_TAXONOMY, canonical_skill_id, skill_label

CERTIFICATION_PATTERNS = (
    ("COMPTIA_A_PLUS", "CompTIA A+", r"\bCompTIA\s+A\+(?!\w)"),
    ("COMPTIA_NETWORK_PLUS", "CompTIA Network+", r"\b(?:CompTIA\s+)?Network\+(?!\w)"),
    ("COMPTIA_SECURITY_PLUS", "CompTIA Security+", r"\b(?:CompTIA\s+)?Security\+(?!\w)"),
    ("CCNA", "CCNA", r"\bCCNA\b"),
    ("AZ_900", "AZ-900", r"\bAZ[- ]?900\b"),
    ("AZ_104", "AZ-104", r"\bAZ[- ]?104\b"),
    ("ITIL", "ITIL", r"\bITIL(?:\s+(?:Foundation|v\d))?\b"),
    ("MCSA", "MCSA", r"\bMCSA\b"),
    ("MCSE", "MCSE", r"\bMCSE\b"),
)

CERT_STRENGTHS = {"REQUIRED", "PREFERRED", "NICE_TO_HAVE"}
CLEARANCE_LEVELS = {"NONE_STATED", "PUBLIC_TRUST", "SECRET", "TOP_SECRET", "TS_SCI", "OTHER"}
WORK_AUTHORIZATION_FLAGS = {
    "US_CITIZENSHIP_REQUIRED",
    "SPONSORSHIP_AVAILABLE",
    "NO_SPONSORSHIP",
    "ALREADY_AUTHORIZED_REQUIRED",
}

def _clean(value):
    return " ".join(str(value or "").split())

def _number(value):
    number = float(value)
    return int(number) if number.is_integer() else number

def _sentence(text, start, end, *, certification_starts=None):
    value = str(text or "")
    delimiters = (".", "\n", ";")
    left_candidates = [value.rfind(delimiter, 0, start) for delimiter in delimiters]
    right_candidates = [
        index for index in (value.find(delimiter, end) for delimiter in delimiters)
        if index >= 0
    ]
    if certification_starts:
        for boundary in re.finditer(r",\s*(?:(?:but|and)\s+)?|\s+and\s+", value, re.I):
            if boundary.end() not in certification_starts:
                continue
            if boundary.start() < start:
                left_candidates.append(boundary.start())
            elif boundary.start() >= end:
                right_candidates.append(boundary.start())
    left = max(left_candidates)
    right = min(right_candidates) if right_candidates else len(value)
    return _clean(value[left + 1:right])
def parse_experience_requirements(text):
    """Return explicit years-of-experience clauses without inventing missing bounds."""
    value = str(text or "")
    results = []
    patterns = (
        re.compile(r"(?P<low>\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:\+\s*)?years?\b", re.I),
        re.compile(r"(?P<low>\d+(?:\.\d+)?)\s*\+\s*years?\b", re.I),
        re.compile(r"(?P<low>\d+(?:\.\d+)?)\s+years?\b", re.I),
    )
    occupied = []
    for pattern in patterns:
        for match in pattern.finditer(value):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            clause = _sentence(value, match.start(), match.end())
            folded = clause.casefold()
            preferred = bool(re.search(r"\b(preferred|ideally|desired|nice\s+to\s+have)\b", folded))
            required = bool(re.search(r"\b(required|minimum|must|need(?:ed)?|requires?)\b", folded))
            low = _number(match.group("low"))
            high = _number(match.group("high")) if "high" in match.groupdict() and match.group("high") else None
            plus = "+" in match.group(0)
            result = {
                "minimum_years": low,
                "maximum_years": None if plus else high,
                "preferred_years": low if preferred else None,
                "requirement_strength": "PREFERRED" if preferred else ("REQUIRED" if required else "UNKNOWN"),
                "raw": _clean(match.group(0)),
                "clause": clause,
            }
            results.append(result)
            occupied.append((match.start(), match.end()))
    results.sort(key=lambda row: value.find(row["raw"]))
    return results

_SKILL_STRENGTH_PATTERNS = (
    ("REQUIRED", re.compile(r"\b(?:required|mandatory|must(?:[-\s]+have)?|shall)\b", re.I)),
    ("PREFERRED", re.compile(r"\b(?:strongly\s+preferred|preferred|desired|ideally)\b", re.I)),
    ("NICE_TO_HAVE", re.compile(r"\b(?:nice[-\s]+to[-\s]+have|bonus|plus)\b", re.I)),
)


def _skill_occurrences(text, recognized):
    allowed = set()
    for item in recognized or []:
        identifier = item.get("id") if isinstance(item, dict) else item
        identifier = canonical_skill_id(identifier)
        if identifier:
            allowed.add(identifier)
    candidates = []
    for identifier in allowed:
        for alias in SKILL_TAXONOMY[identifier][1]:
            pattern = re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)",
                                 0 if alias in {"AD", "Go"} else re.I)
            for match in pattern.finditer(text):
                candidates.append((match.start(), -(match.end() - match.start()),
                                   match.end(), identifier))
    occupied = []
    result = []
    for start, _, end, identifier in sorted(candidates):
        if any(start < right and end > left for left, right in occupied):
            continue
        occupied.append((start, end))
        result.append({"start": start, "end": end, "id": identifier,
                       "skill": skill_label(identifier)})
    return sorted(result, key=lambda row: row["start"])


def _skill_cue_records(value):
    records = []
    for strength, pattern in _SKILL_STRENGTH_PATTERNS:
        for match in pattern.finditer(value):
            prefix = value[max(0, match.start() - 50):match.start()]
            suffix = value[match.end():match.end() + 12]
            is_negated = bool(re.search(
                r"\b(?:not|never|no longer)\s+(?:(?:strictly|necessarily|a|an|the|usually|generally|always)\s+)*(?:be\s+)?$|"
                r"\b(?:isn't|aren't|wasn't|weren't|mustn't|shouldn't)\s+(?:be\s+)?$",
                prefix, re.I,
            )) or bool(re.search(r"^\s+not\b", suffix, re.I))
            records.append({"strength": strength, "start": match.start(),
                            "end": match.end(), "negated": is_negated})
    return records


def _skill_cue_strength(value):
    """Classify only explicit, non-conflicting local cues."""
    records = _skill_cue_records(value)
    matches = [row["strength"] for row in records if not row["negated"]]
    negated = {row["strength"] for row in records if row["negated"]}
    classes = set(matches)
    if len(classes) == 1 and not classes.intersection(negated):
        return next(iter(classes))
    if classes or negated:
        return "UNKNOWN"
    return "UNKNOWN"


def _skill_clause_rows(clause, occurrences):
    """Keep list scope local; an unqualified list member makes the clause ambiguous."""
    if not occurrences:
        return {}
    if len({row["id"] for row in occurrences}) == 1:
        strength = _skill_cue_strength(clause)
        return {occurrences[0]["id"]: [strength] * len(occurrences)}

    per_id = {}
    strengths = []
    cues = _skill_cue_records(clause)
    assignments = {index: [] for index in range(len(occurrences))}
    ambiguous = set()
    for cue in cues:
        distances = []
        for index, occurrence in enumerate(occurrences):
            distance = (occurrence["start"] - cue["end"] if cue["end"] <= occurrence["start"]
                        else cue["start"] - occurrence["end"] if cue["start"] >= occurrence["end"]
                        else 0)
            distances.append((distance, index))
        nearest = min((row[0] for row in distances), default=None)
        owners = [index for distance, index in distances if distance == nearest]
        if len(owners) != 1:
            ambiguous.update(owners)
        else:
            assignments[owners[0]].append(cue)
    for index, occurrence in enumerate(occurrences):
        assigned = assignments[index]
        classes = {row["strength"] for row in assigned if not row["negated"]}
        negated_classes = {row["strength"] for row in assigned if row["negated"]}
        strength = (next(iter(classes)) if len(classes) == 1
                    and not classes.intersection(negated_classes) and index not in ambiguous
                    else "UNKNOWN")
        strengths.append(strength)
        per_id.setdefault(occurrence["id"], []).append(strength)
    # A skill with no nearby qualifier makes the comma/list scope ambiguous.
    # Explicitly negated cues are scoped evidence and should not erase a
    # different skill's independently classified strength.
    if any(not assignments[index] for index in range(len(occurrences))):
        return {identifier: ["UNKNOWN"] * len(values)
                for identifier, values in per_id.items()}
    return per_id


def parse_skill_requirements(text, recognized_skills):
    """Classify detected taxonomy skills only when nearby wording is unambiguous."""
    value = str(text or "")
    occurrences = _skill_occurrences(value, recognized_skills)
    if not occurrences:
        return []
    boundaries = [match.start() for match in re.finditer(r"[.!?;\n]", value)
                  if not any(row["start"] <= match.start() < row["end"]
                             for row in occurrences)]
    clauses = []
    start = 0
    for boundary in boundaries + [len(value)]:
        clauses.append((start, boundary, value[start:boundary]))
        start = boundary + 1
    strengths_by_id = {}
    for start, end, clause in clauses:
        local_occurrences = [row for row in occurrences
                             if start <= row["start"] and row["end"] <= end]
        if not local_occurrences:
            continue
        relative = [{**row, "start": row["start"] - start,
                     "end": row["end"] - start} for row in local_occurrences]
        for identifier, strengths in _skill_clause_rows(clause, relative).items():
            strengths_by_id.setdefault(identifier, []).extend(strengths)
    rows = []
    for identifier in dict.fromkeys(row["id"] for row in occurrences):
        strengths = strengths_by_id.get(identifier, ["UNKNOWN"])
        strength = (strengths[0] if strengths and strengths[0] != "UNKNOWN"
                    and all(value == strengths[0] for value in strengths) else "UNKNOWN")
        rows.append({"id": identifier, "skill": skill_label(identifier),
                     "strength": strength})
    return rows


def experience_requirement_summary(text):
    rows = parse_experience_requirements(text)
    equivalent = bool(re.search(r"\bequivalent\s+(?:experience|combination)\b", str(text or ""), re.I))
    required = [row for row in rows if row["requirement_strength"] == "REQUIRED"]
    preferred = [row for row in rows if row["requirement_strength"] == "PREFERRED"]
    return {
        "requirements": rows,
        "minimum_years": max((row["minimum_years"] for row in required), default=None),
        "maximum_years": max((row["maximum_years"] for row in required if row["maximum_years"] is not None), default=None),
        "preferred_years": max((row["preferred_years"] for row in preferred if row["preferred_years"] is not None), default=None),
        "equivalent_experience_accepted": equivalent,
    }
def _cert_strength(clause, certification_mention):
    folded = clause.casefold()
    for match in re.finditer(r"\b(required|must|mandatory|shall)\b", folded):
        prefix = folded[max(0, match.start() - 40):match.start()]
        suffix = folded[match.end():match.end() + 12]
        negated = (
            match.group(0) in {"required", "mandatory"}
            and bool(re.search(
                r"\b(?:not|never|no longer)\s+(?:(?:strictly|necessarily|a|an|the|usually|generally|always)\s+)*(?:be\s+)?$|"
                r"\b(?:isn't|aren't|wasn't|weren't)\s+$|"
                r"\b(?:not|never)\s+(?:strictly\s+)?(?:required|mandatory)\s+(?:or|and)\s+(?:(?:is|are)\s+)?$",
                prefix,
            ))
        ) or (
            match.group(0) == "required"
            and bool(re.match(
                r"^\s*no\s+" + re.escape(certification_mention.casefold())
                + r"(?:\s+certification)?\s+(?:is\s+)?$",
                folded[:match.start()],
            ))
        ) or (
            match.group(0) in {"must", "shall"}
            and (
                bool(re.match(r"\s+not\b", suffix))
                or (
                    match.group(0) == "must"
                    and bool(re.search(r"\b(?:not|never)\s+(?:a\s+)?$", prefix))
                    and bool(re.match(r"[-\s]?have\b", suffix))
                )
            )
        )
        if not negated:
            return "REQUIRED"
    if re.search(r"\b(preferred|desired|strongly preferred)\b", folded):
        return "PREFERRED"
    if re.search(r"\b(nice\s+to\s+have|plus|bonus)\b", folded):
        return "NICE_TO_HAVE"
    return "UNKNOWN"

def parse_certifications(text):
    value = str(text or "")
    found = []
    seen = set()
    certification_starts = {
        match.start()
        for _, _, pattern in CERTIFICATION_PATTERNS
        for match in re.finditer(pattern, value, re.I)
    }
    for identifier, name, pattern in CERTIFICATION_PATTERNS:
        for match in re.finditer(pattern, value, re.I):
            clause = _sentence(
                value, match.start(), match.end(),
                certification_starts=certification_starts,
            )
            key = (identifier, match.start())
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "id": identifier,
                "name": name,
                "strength": _cert_strength(clause, match.group(0)),
                "raw": _clean(match.group(0)),
                "clause": clause,
            })
    found.sort(key=lambda row: value.casefold().find(row["raw"].casefold()))
    return found

def parse_clearance(text):
    value = str(text or "")
    patterns = (
        ("TS_SCI", r"\b(?:TS\s*/\s*SCI|Top\s+Secret\s*/\s*SCI)\b"),
        ("TOP_SECRET", r"\bTop\s+Secret\b"),
        ("PUBLIC_TRUST", r"\bPublic\s+Trust\b"),
        ("SECRET", r"(?<!Top\s)\bSecret\b"),
    )
    for level, pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            clause = _sentence(value, match.start(), match.end())
            return {"level": level, "raw": _clean(match.group(0)), "clause": clause}
    generic = re.search(r"\b(?:security\s+)?clearance\b", value, re.I)
    if generic:
        return {"level": "OTHER", "raw": _clean(generic.group(0)), "clause": _sentence(value, generic.start(), generic.end())}
    return {"level": "NONE_STATED", "raw": None, "clause": None}
def parse_work_authorization(text):
    value = str(text or "")
    patterns = (
        ("US_CITIZENSHIP_REQUIRED", r"\b(?:U\.?S\.?|United\s+States)\s+citizenship\s+(?:is\s+)?required\b|\bmust\s+be\s+(?:a\s+)?U\.?S\.?\s+citizen\b"),
        ("SPONSORSHIP_AVAILABLE", r"\b(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)\b|\bwill\s+sponsor\b"),
        ("NO_SPONSORSHIP", r"\bno\s+(?:visa\s+)?sponsorship\b|\b(?:visa\s+)?sponsorship\s+(?:is\s+)?not\s+(?:available|provided|offered)\b|\bwill\s+not\s+sponsor\b"),
        ("ALREADY_AUTHORIZED_REQUIRED", r"\bmust\s+(?:already\s+)?be\s+(?:legally\s+)?authorized\s+to\s+work\b|\b(?:current|existing)\s+work\s+authorization\s+required\b"),
    )
    rows = []
    for flag, pattern in patterns:
        for match in re.finditer(pattern, value, re.I):
            rows.append({
                "flag": flag,
                "raw": _clean(match.group(0)),
                "clause": _sentence(value, match.start(), match.end()),
            })
    rows.sort(key=lambda row: value.casefold().find(row["raw"].casefold()))
    flags = []
    for row in rows:
        if row["flag"] not in flags:
            flags.append(row["flag"])
    return {"flags": flags, "statements": rows}

def parse_job_requirements(text):
    return {
        "experience": experience_requirement_summary(text),
        "certifications": parse_certifications(text),
        "clearance": parse_clearance(text),
        "work_authorization": parse_work_authorization(text),
    }
