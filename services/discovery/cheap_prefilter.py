"""Cheap, deterministic title and keyword filtering before job analysis.

Rows in every result bucket are the original source/canonical posting objects.
Filtering controls which rows proceed to expensive work; it never deletes or
rewrites captured posting data.
"""
from collections.abc import Mapping
from itertools import islice


DEFAULT_MAX_CANDIDATES = 500
MAX_CANDIDATES = 10_000
MAX_POSTINGS = 100_000
MAX_TEXT_FIELD_LENGTH = 50_000
MAX_TERMS = 100
MAX_TERM_LENGTH = 200

_FILTER_FIELDS = (
    "include_titles",
    "exclude_titles",
    "required_keywords",
    "optional_keywords",
    "excluded_keywords",
    "exact_phrases",
)


def _terms(criteria, field):
    values = criteria.get(field, [])
    if not isinstance(values, (list, tuple)) or len(values) > MAX_TERMS:
        raise ValueError(f"{field} must be a bounded list of text terms")
    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TERM_LENGTH:
            raise ValueError(f"{field} contains an invalid text term")
        cleaned.append(value.strip().casefold())
    return tuple(cleaned)


def _aliases(criteria):
    raw = criteria.get("technology_aliases") or {}
    if not isinstance(raw, Mapping) or len(raw) > MAX_TERMS:
        raise ValueError("technology_aliases must be a bounded mapping")
    result = {}
    for canonical, aliases in raw.items():
        if not isinstance(canonical, str) or not canonical.strip() or len(canonical.strip()) > 100:
            raise ValueError("technology_aliases contains an invalid canonical term")
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 30:
            raise ValueError("technology_aliases contains an invalid alias list")
        values = {canonical.strip().casefold()}
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > 100:
                raise ValueError("technology_aliases contains an invalid alias")
            values.add(alias.strip().casefold())
        result[canonical.strip().casefold()] = values
    return result


def _corpus(row):
    values = [
        row.get("title"),
        row.get("company"),
        row.get("description"),
        row.get("location"),
    ]
    skills = row.get("skills") or []
    if isinstance(skills, set):
        values.extend(sorted(skills, key=lambda value: str(value).casefold()))
    elif isinstance(skills, (list, tuple)):
        values.extend(skills)
    else:
        values.append(skills)
    truncated = False
    folded_values = []
    for value in values:
        text = str(value or "")
        if len(text) > MAX_TEXT_FIELD_LENGTH:
            truncated = True
            text = text[:MAX_TEXT_FIELD_LENGTH]
        folded_values.append(text.casefold())
    return " ".join(folded_values), truncated


def _contains(term, corpus, aliases):
    variants = aliases.get(term, {term})
    return any(variant in corpus for variant in variants)


def _rejection_reasons(row, filters, aliases):
    raw_title = str(row.get("title") or "")
    title_uncertain = len(raw_title) > MAX_TEXT_FIELD_LENGTH
    title = raw_title[:MAX_TEXT_FIELD_LENGTH].casefold()
    include_titles = filters["include_titles"]
    if include_titles and not title_uncertain and not any(term in title for term in include_titles):
        return ("TITLE_NOT_INCLUDED",)
    if not title_uncertain and any(term in title for term in filters["exclude_titles"]):
        return ("TITLE_EXCLUDED",)

    corpus, truncated = _corpus(row)
    if truncated:
        # An incomplete corpus cannot safely prove a keyword mismatch.
        return ()
    reasons = []
    for term in filters["required_keywords"]:
        if not _contains(term, corpus, aliases):
            reasons.append("REQUIRED_KEYWORD_MISSING:" + term)
    for term in filters["excluded_keywords"]:
        if _contains(term, corpus, aliases):
            reasons.append("EXCLUDED_KEYWORD:" + term)
    for phrase in filters["exact_phrases"]:
        if phrase not in corpus:
            reasons.append("EXACT_PHRASE_MISSING:" + phrase)
    return tuple(reasons)


def prefilter_postings(postings, criteria, *, max_candidates=DEFAULT_MAX_CANDIDATES):
    """Partition postings into candidates, rejected rows, and deferred candidates.

    Criteria apply only the inexpensive title/keyword subset used by the
    existing search profile contract. Optional keywords are ranking hints, so
    they never reject a posting. Candidate limiting is explicit and preserves
    source order; plausible rows beyond the limit are returned as deferred.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be an integer from 1 to {MAX_CANDIDATES}")
    if not isinstance(criteria, Mapping):
        raise ValueError("criteria must be a mapping")

    filters = {field: _terms(criteria, field) for field in _FILTER_FIELDS}
    aliases = _aliases(criteria)
    rows = list(islice(postings, MAX_POSTINGS + 1))
    if len(rows) > MAX_POSTINGS:
        raise ValueError(f"postings exceeds the {MAX_POSTINGS} row processing bound")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("postings must contain mapping rows")

    candidates = []
    rejected = []
    deferred = []
    for row in rows:
        reasons = _rejection_reasons(row, filters, aliases)
        if reasons:
            rejected.append(row)
        elif len(candidates) < max_candidates:
            candidates.append(row)
        else:
            deferred.append(row)

    return {
        "candidates": candidates,
        "rejected": rejected,
        "deferred": deferred,
        "stage_counts": {"captured": len(rows), "prefiltered": len(candidates)},
    }
