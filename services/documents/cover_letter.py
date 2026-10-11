"""Deterministic, evidence-bound cover-letter drafts."""
import hashlib
import re
from urllib.parse import urlsplit

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
_RESPONSIBILITY_HEADING = re.compile(
    r"^(?:(?:your|key|role|job)\s+)*(?:responsibilit\w*|what you(?:'|’)ll do|"
    r"what you will do|duties|day to day)$",
    re.I,
)
_SECTION_HEADING = re.compile(
    r"^(?:(?:your|key|role|job|required|preferred|additional|basic|minimum|essential|core)\s+)*"
    r"(?:responsibilit\w*|what you(?:'|’)ll do|what you will do|duties|day to day|"
    r"requirements|qualifications|what you bring|preferred skills|benefits)"
    r"(?:\s*:\s*.*)?$",
    re.I,
)


def _approved_company_context(job):
    attributes = job.get("attributes") if isinstance(job.get("attributes"), dict) else {}
    context = attributes.get("approved_company_context")
    if not isinstance(context, dict):
        return None
    required = ("id", "company_id", "company_source_id", "version", "content_origin",
                "content_status", "source_host",
                "content", "source_url", "verified_domain", "source_identity_method",
                "identity_status", "content_sha256", "reviewed_at")
    if any(context.get(key) in (None, "") for key in required):
        return None
    content = context.get("content")
    if (context.get("content_origin") != "USER_PROVIDED"
            or context.get("content_status") != "UNVERIFIED"
            or context.get("identity_status") != "VERIFIED"
            or context.get("user_reviewed") is not True
            or context.get("user_approved") is not True
            or not isinstance(content, str) or not content.strip() or len(content) > 4000
            or type(context.get("version")) is not int or context["version"] < 1
            or (job.get("company_id") and context.get("company_id") != job.get("company_id"))):
        return None
    if context.get("source_identity_method") not in {
        "FIRST_PARTY_JOB_LINK", "VERIFIED_EXISTING_DOMAIN", "ATS_COMPANY_METADATA",
        "OFFICIAL_CAREER_REDIRECT", "OFFICIAL_STRUCTURED_METADATA",
    }:
        return None
    try:
        parsed = urlsplit(context["source_url"])
        host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
        port = parsed.port
    except (TypeError, ValueError):
        return None
    domain = str(context["verified_domain"]).casefold().rstrip(".").removeprefix("www.")
    if (parsed.scheme.casefold() not in {"http", "https"} or not host or not domain
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 80, 443)
            or context["source_host"] != host
            or not (host == domain or host.endswith("." + domain))):
        return None
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", str(context["content_sha256"])) or digest != context["content_sha256"]:
        return None
    return dict(context)


def _normalized_choice(value, choices):
    choice = " ".join(str(value or "").strip().casefold().replace("_", " ").split())
    return choice if choice in choices else None


def _responsibilities(description):
    rows = []
    in_section = False
    lines = str(description or "").replace("\r", "\n").splitlines()
    has_explicit_section = any(
        _SECTION_HEADING.fullmatch(
            re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).rstrip(":").strip()
        )
        for line in lines
    )
    for line in lines:
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
        if _RESPONSIBILITY_HEADING.fullmatch(value.rstrip(":").strip()):
            in_section = True
            continue
        if _SECTION_HEADING.fullmatch(value.strip()):
            in_section = False
            continue
        if in_section and len(value) <= 80 and value.endswith(":"):
            in_section = False
        first = (_WORD.findall(value) or [""])[0].casefold().rstrip(".")
        action_line = first in _ACTION_VERBS or value.casefold().startswith(("you will ", "you'll "))
        if in_section and not action_line and not bullet:
            in_section = False
        if (action_line and (in_section or not has_explicit_section)) or (in_section and bullet):
            rows.append(value[:500])
        if len(rows) >= 5:
            break
    return rows


def _tokens(value):
    return {token for word in _WORD.findall(str(value or ""))
            if (token := word.casefold().rstrip(".")) and token not in _STOP}


def build_cover_letter(profile, job, facts, style="professional", length="normal",
                       selected_stories=None):
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
    selected_stories = list(selected_stories or [])
    story_refs = []
    story_paragraphs = []
    for index, story in enumerate(selected_stories, 1):
        text = story.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PreparationError("STORY_NOT_SELECTABLE", "A selected story has no reviewed text")
        story_refs.append({
            "story_id": story["story_id"],
            "revision": story["revision"],
            "title": story["title"],
            "profile_version": story["profile_version"],
            "fact_ids": list(story["fact_ids"]),
            "fact_fingerprints": [dict(item) for item in story["fact_fingerprints"]],
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
        story_paragraphs.extend((
            f"Selected user-authored story {index} (verbatim; review before use):",
            text,
        ))
    company_context = _approved_company_context(job)
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
        *story_paragraphs,
        *(["Company context (USER_PROVIDED; CONTENT UNVERIFIED; SOURCE IDENTITY VERIFIED; reviewed by you; source: "
           + company_context["source_url"] + "): " + company_context["content"]]
          if company_context else []),
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
        "company_context": company_context,
        "job_snapshot": {"title": title, "company": company, "skills": skills,
                         "company_context": company_context,
                         "attributes": {"skill_evidence": attributes.get("skill_evidence"),
                                        "company_identity": attributes.get("company_identity", "SOURCE_LISTED"),
                                        "required_skills": required, "preferred_skills": preferred,
                                        "approved_company_context": company_context},
                         "responsibility_evidence": responsibilities},
    }
    if story_refs:
        context["story_refs"] = story_refs
        context["review_required"] = True
    return paragraphs, context
