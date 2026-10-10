"""Bounded positive-evidence extraction for approved career skill/experience facts.

This is a deterministic safeguard for explicit English denials and aspirations,
not general natural-language understanding or a new qualification inference.
Conflicting approved facts require review; raw career text is never returned.
"""
from collections.abc import Mapping
from math import isfinite
import re

from services.discovery.skill_taxonomy import canonical_skill_id, extract_skill_mentions, skill_label

EXPERIENCE_KINDS=frozenset({"experience","technology_experience","responsibility","project","accomplishment"})
_CLAUSES=re.compile(r"[;\n!?]|\.(?:\s|$)|\b(?:but|however|whereas|although)\b",re.I)
_NEGATIVE=re.compile(r"\b(?:no|not|never|without|lack(?:s|ed|ing)?|haven't|hasn't|hadn't|don't|doesn't|didn't|cannot|can't|unable)\b",re.I)
_ASPIRATION=re.compile(r"\b(?:interested\s+in|want(?:s)?\s+to|plan(?:s)?\s+to|hope(?:s)?\s+to|would\s+like\s+to|seeking\s+to|learning|studying)\b",re.I)
_SCOPE_WORDS=frozenset("""a an any the prior previous current currently direct relevant professional practical real
 production hands on experience knowledge skill skills exposure familiarity background expertise proficiency training
 using use used worked working with in of on to and or have has had do does did know knowing familiar proficient
 comfortable supported supporting managed managing administered learned learn trained training use yet i am is are""".split())
_AFTER=re.compile(r"^\s*(?:(?:and|or)\s+)*(?:(?:experience|knowledge|skills?|proficiency|familiarity|exposure|background)\s*)?"
                  r"(?:(?:is|are|was)\s*)?[:=??-]?\s*(?:none|unknown|unverified|not\s+(?:known|verified|demonstrated|present)|no\s+(?:experience|knowledge|exposure))\b",re.I)


def _without_mentions(text):
    for mention in reversed(extract_skill_mentions(text)):
        text=text[:mention["start"]]+" "+text[mention["end"]:]
    return text


def _bounded_scope(text):
    words=re.findall(r"[a-z]+",_without_mentions(text).casefold())
    return len(words)<=18 and all(word in _SCOPE_WORDS for word in words)


def _classify(text):
    positive,negative,uncertain=set(),set(),set()
    for clause in _CLAUSES.split(text.replace("?","'")):
        mentions=extract_skill_mentions(clause)
        for mention in mentions:
            prefix=clause[:mention["start"]]
            # A comma can separate a positive assertion from a later negative
            # assertion, so suffix-based denials do not cross that boundary.
            suffix=clause[mention["end"]:].split(",",1)[0]
            denied=any(_bounded_scope(prefix[marker.end():]) for marker in _NEGATIVE.finditer(prefix))
            denied=denied or bool(_AFTER.search(_without_mentions(suffix)))
            aspirational=any(_bounded_scope(prefix[marker.end():]) for marker in _ASPIRATION.finditer(prefix))
            if denied:
                negative.add(mention["id"])
            elif aspirational:
                uncertain.add(mention["id"])
            else:
                positive.add(mention["id"])
    return positive,negative,uncertain


def collect_positive_skill_evidence(facts, kinds):
    """Keep approval/source/confidence gates and withhold explicit contradictions.

    Only recognized taxonomy skills receive clause-level safeguards. Unknown,
    clean exact skill labels retain legacy exact-match behavior. The result is
    transient, with labels/reason codes only; no approval/fact is changed.
    """
    positive,negative,uncertain,exact=set(),set(),set(),set()
    for fact in facts if isinstance(facts,(list,tuple)) else []:
        if not isinstance(fact,Mapping) or fact.get("kind") not in kinds:
            continue
        text,source,confidence=fact.get("text"),fact.get("source"),fact.get("confidence")
        if (fact.get("verified") is not True or fact.get("user_approved") is not True
                or type(confidence) not in (int,float) or not isfinite(confidence) or not 80<=confidence<=100
                or not isinstance(text,str) or not text.strip() or not isinstance(source,str) or not source.strip()):
            continue
        yes,no,maybe=_classify(text)
        positive.update(yes);negative.update(no);uncertain.update(maybe)
        if fact.get("kind")=="skill" and not no and not maybe:
            # Unrecognized prose containing denial/aspiration is not a new exact
            # candidate skill. Recognized positive idioms still use their labels.
            if not _NEGATIVE.search(text) and not _ASPIRATION.search(text):
                exact.add(text.strip().casefold())
    supported=positive-negative
    exact={text for text in exact if canonical_skill_id(text) not in negative}
    return {
        "labels":sorted(skill_label(identifier) for identifier in supported),
        "exact":exact,
        "withheld_ids":(negative|uncertain)-supported,
        "conflicting_ids":positive&negative,
    }
