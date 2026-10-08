"""Deterministic, evidence-bound cover-letter drafts."""
import re

from services.documents.errors import PreparationError


STYLES = ("professional", "warm", "technical", "direct", "conversational", "formal")
LENGTHS = ("very short", "concise", "normal", "detailed")
COVER_LETTER_TEMPLATE = "job-specific-approved-facts-v1"
_LIMITS = {
    "very short": (1, 1, 1),
    "concise": (1, 2, 2),
    "normal": (2, 3, 3),
    "detailed": (4, 5, 5),
}
_OPENINGS = {
    "professional": "I am applying for this position based on the evidence below.",
    "warm": "I would be glad to discuss how the evidence below fits this position.",
    "technical": "I am applying with the technical evidence below for this position.",
    "direct": "Please consider my application based on the evidence below.",
    "conversational": "I would like to be considered for this position; the evidence below is relevant.",
    "formal": "Please accept this application, supported by the evidence below.",
}
_CLOSINGS = {
    "professional": "I welcome a conversation about this evidence and the role.",
    "warm": "Thank you for reviewing this information; I welcome a conversation about the role.",
    "technical": "I welcome a discussion of these skills and role requirements.",
    "direct": "I welcome a discussion about the role.",
    "conversational": "Thank you for taking a look. I would be happy to discuss the role.",
    "formal": "Thank you for your consideration. I welcome further discussion.",
}
_ACTION_VERBS = {
    "administer", "analyze", "automate", "build", "collaborate", "configure", "coordinate",
    "create", "deploy", "design", "develop", "improve", "maintain", "manage", "monitor",
    "operate", "own", "resolve", "secure", "support", "troubleshoot", "upgrade",
}
_STOP = {"and", "are", "for", "from", "have", "into", "that", "the", "their", "this", "with", "your"}
_WORD = re.compile(r"[a-z0-9+#.]{2,}", re.I)


def _normalized_choice(value, choices):
    choice = " ".join(str(value or "").strip().casefold().replace("_", " ").split())
    return choice if choice in choices else None


def _responsibilities(description):
    rows = []
    in_section = False
    for line in str(description or "").replace("\r", "\n").splitlines():
        bullet = bool(re.match(r"^\s*(?:[-*•]+|\d+[.)])\s*", line))
        value = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()
        if not value:
            continue
        tagged = re.search(r"\b(?:responsibilities|what you(?:'|’)ll do|what you will do|duties)\s*:\s*(.+)$", value, re.I)
        if tagged:
            value = tagged.group(1).strip()
            in_section = True
            if not value:
                continue
        heading = value.rstrip(":").strip().casefold()
        if any(marker in heading for marker in ("responsibilit", "what you'll do", "what you will do", "duties", "day to day")):
            in_section = True
            continue
        if in_section and any(marker in heading for marker in ("requirements", "qualifications", "what you bring", "preferred skills", "benefits")):
            in_section = False
        if in_section and len(value) <= 80 and value.endswith(":"):
            in_section = False
        first = (_WORD.findall(value) or [""])[0].casefold().rstrip(".")
        action_line = first in _ACTION_VERBS or value.casefold().startswith(("you will ", "you'll "))
        if in_section and not action_line and not bullet:
            in_section = False
        if action_line or (in_section and bullet):
            rows.append(value[:500])
        if len(rows) >= 5:
            break
    return rows


def _tokens(value):
    return {token for word in _WORD.findall(str(value or ""))
            if (token := word.casefold().rstrip(".")) and token not in _STOP}


def build_cover_letter(profile, job, facts, style="professional", length="normal"):
    style = _normalized_choice(style, STYLES)
    length = _normalized_choice(length, LENGTHS)
    if not style or not length:
        raise PreparationError("COVER_LETTER_OPTIONS_INVALID", "Choose a supported cover-letter style and length")

    title = " ".join(str(job.get("title") or "").split())
    company = " ".join(str(job.get("company") or "").split())
    skills = list(dict.fromkeys(" ".join(str(value).split()) for value in (job.get("skills") or [])
                                if isinstance(value, str) and value.strip()))
    attributes = job.get("attributes") if isinstance(job.get("attributes"), dict) else {}
    required = list(dict.fromkeys(" ".join(str(value).split()) for value in (
        attributes.get("required_skills") or (skills if attributes.get("skill_evidence") == "structured_required_skills" else []))
        if isinstance(value, str) and value.strip()))
    preferred = list(dict.fromkeys(" ".join(str(value).split()) for value in (attributes.get("preferred_skills") or [])
                                   if isinstance(value, str) and value.strip()))
    responsibilities = job.get("responsibility_evidence") or _responsibilities(job.get("description"))
    responsibilities = list(dict.fromkeys(" ".join(str(value).split()) for value in responsibilities
                                          if isinstance(value, str) and value.strip()))
    if not title or not company or not skills or not responsibilities:
        raise PreparationError("COVER_LETTER_JOB_CONTEXT_MISSING",
                               "This letter needs a job title, company, posting skill terms, and a clear responsibility excerpt")

    eligible = [row for row in facts if row.get("verified") is True and row.get("user_approved") is True
                and row.get("confidence", 0) >= 80 and str(row.get("source") or "").strip()
                and str(row.get("text") or "").strip()]
    skill_terms = set().union(*(_tokens(value) for value in skills))
    responsibility_terms = set().union(*(_tokens(value) for value in responsibilities))
    target_terms = (skill_terms | responsibility_terms) - _STOP
    ranked = []
    for index, fact in enumerate(eligible):
        overlap = _tokens(fact["text"]) & target_terms
        if overlap:
            normalized = {**fact, "fact_id": fact.get("fact_id") or fact.get("id")}
            if normalized["fact_id"]:
                ranked.append((-len(overlap), index, normalized, sorted(overlap)))
    # Rank by the count of exact normalized posting tokens, retaining source
    # order for equal scores so identical inputs always produce identical drafts.
    ranked.sort(key=lambda row: (row[0], row[1]))
    if not ranked:
        raise PreparationError("COVER_LETTER_RELEVANT_FACTS_MISSING",
                               "No approved, sourced career fact matches these posting details; review facts or choose another job")

    responsibility_limit, skill_limit, fact_limit = _LIMITS[length]
    chosen_responsibilities = responsibilities[:responsibility_limit]
    chosen_skills = list(dict.fromkeys(required + preferred + skills))[:skill_limit]
    chosen_facts = ranked[:fact_limit]
    if required:
        skill_paragraph = "The posting explicitly requires: " + "; ".join(required[:skill_limit]) + "."
    elif preferred:
        skill_paragraph = ("The posting explicitly prefers: " + "; ".join(preferred[:skill_limit]) + ".")
    else:
        skill_paragraph = ("The posting mentions these skill terms without stating their strength: "
                           + "; ".join(chosen_skills) + ".")
    paragraphs = [
        str(profile.get("name") or "").strip(),
        f"Application for {title} at {company}",
        _OPENINGS[style],
        "The posting describes this work: " + " ".join(chosen_responsibilities),
        skill_paragraph,
        "Approved career facts relevant to these details: " + "; ".join(row[2]["text"] for row in chosen_facts) + ".",
        _CLOSINGS[style],
    ]
    context = {
        "mode": "JOB_SPECIFIC_APPROVED_FACTS",
        "style": style,
        "length": length,
        "fact_ids": [row[2]["fact_id"] for row in chosen_facts],
        "matched_terms": {row[2]["fact_id"]: row[3] for row in chosen_facts},
        "responsibilities": chosen_responsibilities,
        "skills": chosen_skills,
        "skill_strengths": {value: "REQUIRED" if value in required else "PREFERRED" if value in preferred else "UNKNOWN"
                            for value in chosen_skills},
        "company_identity": attributes.get("company_identity", "SOURCE_LISTED"),
        "job_snapshot": {"title": title, "company": company, "skills": skills,
                         "attributes": {"skill_evidence": attributes.get("skill_evidence"),
                                        "company_identity": attributes.get("company_identity", "SOURCE_LISTED"),
                                        "required_skills": required, "preferred_skills": preferred},
                         "responsibility_evidence": responsibilities},
    }
    return paragraphs, context
