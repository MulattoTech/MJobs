"""Pure, review-first Workday page classification and transition decisions.

This module consumes caller-supplied page observations. It never drives a
browser, stores application data, or submits an application.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit


_STEP_ORDER = (
    "my_information",
    "my_experience",
    "application_questions",
    "voluntary_disclosures",
    "review",
)
WORKDAY_STEP_ORDER = _STEP_ORDER
_HEADING_STEPS = {
    "my information": "my_information",
    "my experience": "my_experience",
    "application questions": "application_questions",
    "voluntary disclosures": "voluntary_disclosures",
    "review": "review",
}
_FINAL_ACTIONS = {"SUBMIT", "FINAL_SUBMIT", "SUBMIT_APPLICATION"}
_ALLOWED_ACTIONS = {"NEXT", "PREVIOUS", "REFRESH", "RESUME", *_FINAL_ACTIONS}
_MAX_TEXT_ITEMS = 40
_MAX_TEXT_LENGTH = 256

# This slice is deliberately a classifier/decision helper, not an ATS driver.
CAPABILITIES = {
    "step_classification": True,
    "observed_transition_decisions": True,
    "refresh_and_resume_safety_decisions": True,
    "browser_interaction": False,
    "form_entry": False,
    "document_upload": False,
    "submission": False,
}


def _text_list(value):
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_TEXT_ITEMS:
        return None
    if any(not isinstance(item, str) or len(item) > _MAX_TEXT_LENGTH for item in value):
        return None
    return [re.sub(r"\s+", " ", item).strip().casefold() for item in value]


def workday_origin(url):
    """Return a canonical HTTPS Workday origin for route binding, or None."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.casefold() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        host = parsed.hostname.casefold().rstrip(".")
        if not (host.endswith(".myworkdayjobs.com") or host.endswith(".myworkday.com")):
            return None
        port = parsed.port
        return f"https://{host}" + (f":{port}" if port is not None and port != 443 else "")
    except (TypeError, ValueError):
        return None


def workday_route_digest(url):
    """Return an opaque stable digest for a Workday origin and exact path.

    Query strings and fragments are excluded because they can contain
    session-specific or user data. URL paths remain case-sensitive and
    numeric segments are not normalized: those segments may identify distinct
    employer tenants or requisitions that must not share durable state.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        origin = workday_origin(url)
        if origin is None:
            return None
        identity = json.dumps(
            ["workday-route-v1", origin, parsed.path],
            separators=(",", ":"), ensure_ascii=True,
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None


def _fingerprint(url, headings, labels, actions):
    try:
        origin = workday_origin(url)
        route_digest = workday_route_digest(url)
        if origin is None or route_digest is None:
            return None
        # Query strings and fragments can contain session and user data. They
        # are deliberately excluded from this structural page fingerprint.
        structure = {"origin": origin, "route_digest": route_digest,
                     "headings": headings, "field_labels": labels, "actions": actions}
        encoded = json.dumps(structure, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None


def classify_step(observation: Mapping) -> dict:
    """Classify only familiar Workday step headings with structural landmarks.

    Input keys are ``url``, ``headings``, ``field_labels``, ``actions`` and
    optional boolean ``stale``. Unknown or malformed observations stay UNKNOWN.
    Fingerprints omit URL query strings and fragments.
    """
    unknown = {"status": "UNKNOWN", "step": None, "fingerprint": None,
               "origin": None, "stale": False}
    if not isinstance(observation, Mapping):
        return unknown
    url = observation.get("url")
    headings = _text_list(observation.get("headings"))
    labels = _text_list(observation.get("field_labels", []))
    actions = _text_list(observation.get("actions", []))
    stale = observation.get("stale", False)
    if (not isinstance(url, str) or len(url) > 2048 or headings is None
            or labels is None or actions is None or type(stale) is not bool):
        return unknown
    fingerprint = _fingerprint(url, headings, labels, actions)
    if fingerprint is None:
        return unknown
    origin = workday_origin(url)

    matched = {_HEADING_STEPS[heading] for heading in headings if heading in _HEADING_STEPS}
    # A page with multiple known step headings, or no known heading, is
    # ambiguous even if its form fields look familiar.
    if len(matched) != 1:
        return {**unknown, "stale": stale, "fingerprint": fingerprint, "origin": origin}
    return {"status": "KNOWN", "step": matched.pop(), "fingerprint": fingerprint,
            "origin": origin, "stale": stale}


def _valid_classification(value):
    return (isinstance(value, Mapping) and value.get("status") == "KNOWN"
            and value.get("step") in _STEP_ORDER
            and isinstance(value.get("fingerprint"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]) is not None
            and type(value.get("stale", False)) is bool)


def _decision(name, reason, *, execute=False):
    return {"decision": name, "reason": reason, "execute": execute}


def decide_transition(previous_observation: Mapping, current_observation: Mapping,
                      intended_action: str, *, visited_fingerprints=(), interrupted=False,
                      confirmed_state=None) -> dict:
    """Return a safe decision from two observed classifications; perform no action.

    NEXT/PREVIOUS are confirmed only after a changed, known, adjacent page is
    observed. REFRESH asks the caller to refresh and classify again. RESUME is
    accepted only when an interrupted observation exactly matches its supplied
    confirmed state. Final submission actions are always ambiguous and inert.
    """
    if not isinstance(intended_action, str) or intended_action not in _ALLOWED_ACTIONS:
        return _decision("MANUAL_REVIEW", "UNSUPPORTED_ACTION")
    if intended_action in _FINAL_ACTIONS:
        return _decision("AMBIGUOUS_FINAL_ACTION", "FINAL_ACTION_REQUIRES_HUMAN", execute=False)
    if intended_action == "REFRESH":
        return _decision("REFRESH_AND_RECLASSIFY", "REFRESH_REQUIRES_NEW_OBSERVATION")
    if intended_action == "RESUME":
        if not interrupted or not _valid_classification(current_observation):
            return _decision("MANUAL_REVIEW", "RESUME_REQUIRES_CONFIRMED_STATE")
        if (not _valid_classification(confirmed_state)
                or confirmed_state["fingerprint"] != current_observation["fingerprint"]
                or confirmed_state["step"] != current_observation["step"]):
            return _decision("MANUAL_REVIEW", "INTERRUPTED_STATE_MISMATCH")
        if current_observation.get("stale", False):
            return _decision("REFRESH_AND_RECLASSIFY", "STALE_OBSERVATION")
        return _decision("RESUME_FROM_CONFIRMED_STEP", "CONFIRMED_STATE_MATCH")
    if not _valid_classification(previous_observation) or not _valid_classification(current_observation):
        return _decision("MANUAL_REVIEW", "UNKNOWN_OR_INVALID_OBSERVATION")
    if previous_observation.get("stale", False) or current_observation.get("stale", False):
        return _decision("REFRESH_AND_RECLASSIFY", "STALE_OBSERVATION")
    if (not isinstance(visited_fingerprints, (list, tuple, set, frozenset))
            or any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
                   for item in visited_fingerprints)):
        return _decision("MANUAL_REVIEW", "INVALID_VISITED_STATE")
    if current_observation["fingerprint"] == previous_observation["fingerprint"]:
        return _decision("WAIT_FOR_OBSERVED_CHANGE", "PAGE_HAS_NOT_CHANGED")
    if current_observation["fingerprint"] in visited_fingerprints:
        return _decision("LOOP_DETECTED", "OBSERVED_PAGE_REPEATED")

    old_index = _STEP_ORDER.index(previous_observation["step"])
    new_index = _STEP_ORDER.index(current_observation["step"])
    expected_index = old_index + (1 if intended_action == "NEXT" else -1)
    if not 0 <= expected_index < len(_STEP_ORDER) or new_index != expected_index:
        return _decision("MANUAL_REVIEW", "DESTINATION_NOT_COHERENT")
    return _decision("CONFIRMED", "CHANGED_PAGE_MATCHES_EXPECTED_STEP")
