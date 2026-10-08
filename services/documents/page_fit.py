"""Deterministic page-target trimming around an injected PDF page counter.

This module does not estimate layout. A caller supplies a measurement function
that renders the current candidate and returns its actual PDF page count.
"""
from dataclasses import dataclass
import math
from typing import Callable, Mapping, Sequence


_BULLET_KINDS = frozenset({"responsibility", "experience", "project", "accomplishment"})
_OMISSION_REASON = "PAGE_TARGET_FIT_LOWEST_RETENTION_RANK"


@dataclass(frozen=True)
class PageFitResult:
    selected_facts: tuple[dict, ...]
    omitted_facts: tuple[dict, ...]
    page_count: int | None
    page_target: int
    attempts: int
    measurement_available: bool
    target_met: bool
    reason: str | None


def _validated_facts(facts: Sequence[Mapping]) -> list[dict]:
    if not isinstance(facts, (list, tuple)) or len(facts) > 1000:
        raise ValueError("facts must be a list or tuple with at most 1000 entries")
    copied = []
    identifiers = set()
    for fact in facts:
        if not isinstance(fact, Mapping):
            raise ValueError("each fact must be a mapping")
        row = dict(fact)
        identifier = row.get("fact_id")
        kind = row.get("kind")
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128 or
                identifier in identifiers or not isinstance(kind, str) or not kind.strip()):
            raise ValueError("each fact must have a unique fact_id and nonempty kind")
        if not isinstance(row.get("text"), str):
            raise ValueError("each fact must have text")
        for key in ("approved", "protected", "optional_bullet"):
            if type(row.get(key)) is not bool:
                raise ValueError("each fact must declare approved, protected, and optional_bullet")
        rank = row.get("rank")
        if (isinstance(rank, bool) or not isinstance(rank, (int, float)) or
                not math.isfinite(rank)):
            raise ValueError("each fact must have a finite numeric rank")
        explicitly_selected = row.get("explicitly_selected", False)
        if type(explicitly_selected) is not bool:
            raise ValueError("explicitly_selected must be a boolean when supplied")
        identifiers.add(identifier)
        copied.append(row)
    return copied


def fit_to_page_target(
    facts: Sequence[Mapping],
    *,
    page_target: int,
    measure_page_count: Callable[[Sequence[Mapping]], int],
    max_attempts: int = 8,
) -> PageFitResult:
    """Trim lowest-ranked eligible bullets until measured pages meet target.

    `max_attempts` counts measurement calls, including the initial candidate.
    Only approved optional bullets in supported bullet kinds can be omitted;
    protected and explicitly selected facts always remain. Equal ranks trim
    from the end first, preserving earlier source order deterministically.
    If measurement fails or the target remains unmet, original facts are
    returned unchanged. The reported page count then belongs to the original
    candidate's first successful measurement; no omission is applied unless a
    measured candidate meets the target.
    """
    if type(page_target) is not int or not 1 <= page_target <= 20:
        raise ValueError("page_target must be an integer from 1 through 20")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be an integer from 1 through 10")
    if not callable(measure_page_count):
        raise ValueError("measure_page_count must be callable")
    original = _validated_facts(facts)
    removable = sorted(
        (index for index, row in enumerate(original)
         if row["approved"] and row["optional_bullet"] and not row["protected"] and
         not row.get("explicitly_selected", False) and row["kind"] in _BULLET_KINDS),
        key=lambda index: (original[index]["rank"], -index),
    )
    current = list(original)
    omitted = []
    last_page_count = None
    original_page_count = None
    attempts = 0

    while attempts < max_attempts:
        attempts += 1
        try:
            measured = measure_page_count(tuple(dict(row) for row in current))
            if type(measured) is not int or not 1 <= measured <= 1000:
                raise ValueError("page measurement must be a positive integer")
        except Exception:
            return PageFitResult(
                tuple(original), (), original_page_count, page_target, attempts,
                False, False,
                "PAGE_MEASUREMENT_UNAVAILABLE",
            )
        last_page_count = measured
        if original_page_count is None:
            original_page_count = measured
        if measured <= page_target:
            return PageFitResult(
                tuple(current), tuple(omitted), measured, page_target, attempts,
                True, True, None,
            )
        if not removable:
            return PageFitResult(
                tuple(original), (), original_page_count, page_target, attempts,
                True, False, "NO_ELIGIBLE_BULLETS",
            )
        if attempts == max_attempts:
            return PageFitResult(
                tuple(original), (), original_page_count, page_target, attempts,
                True, False, "MAX_RENDER_ATTEMPTS_REACHED",
            )
        remove_index = removable.pop(0)
        removed = original[remove_index]
        current = [row for row in current if row["fact_id"] != removed["fact_id"]]
        omitted.append({"fact_id": removed["fact_id"], "reason": _OMISSION_REASON})

    return PageFitResult(
        tuple(original), (), original_page_count or last_page_count, page_target, attempts,
        True, False, "MAX_RENDER_ATTEMPTS_REACHED",
    )
