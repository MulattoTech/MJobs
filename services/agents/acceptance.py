"""Host-side, value-free acceptance checks for bounded collection results."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionContract:
    items_key: str
    count_key: str | None = None
    expected_count: int | None = None
    tail_key: str | None = None
    expected_tail: str | int | None = None
    required_item_fields: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.items_key, str) or not self.items_key:
            raise ValueError("Collection items key is required")
        if self.count_key is not None and (not isinstance(self.count_key, str) or not self.count_key):
            raise ValueError("Collection count key must be a non-empty string")
        if self.expected_count is not None and (type(self.expected_count) is not int
                                                or self.expected_count < 0):
            raise ValueError("Expected collection count must be a non-negative integer")
        if self.tail_key is not None and (not isinstance(self.tail_key, str) or not self.tail_key):
            raise ValueError("Collection tail key must be a non-empty string")
        if self.expected_tail is not None and self.tail_key is None:
            raise ValueError("An expected tail requires a tail key")
        if not isinstance(self.required_item_fields, tuple) or any(
                not isinstance(key, str) or not key for key in self.required_item_fields):
            raise ValueError("Required collection fields must be a tuple of non-empty strings")


@dataclass(frozen=True)
class CollectionValidation:
    code: str
    observed_count: int | None
    tail_matches: bool | None


def validate_collection_output(output: object, contract: CollectionContract) -> CollectionValidation:
    """Validate result coverage without storing or echoing collected values."""
    if not isinstance(output, dict) or not isinstance(contract, CollectionContract):
        return CollectionValidation("STRUCTURE_INVALID", None, None)
    items = output.get(contract.items_key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return CollectionValidation("STRUCTURE_INVALID", None, None)
    stated_count = None
    if contract.count_key is not None:
        stated_count = output.get(contract.count_key)
        if stated_count is not None and (type(stated_count) is not int or stated_count < 0):
            return CollectionValidation("STRUCTURE_INVALID", None, None)
    if not items:
        if contract.count_key is not None and stated_count is None:
            return CollectionValidation("COUNT_UNKNOWN", None, None)
        if contract.expected_count not in (None, 0) or stated_count not in (None, 0):
            return CollectionValidation("COUNT_MISMATCH", 0, None)
        if contract.expected_count == 0 and stated_count == 0:
            return CollectionValidation("OUTPUT_VALID", 0, None)
        return CollectionValidation("OUTPUT_EMPTY", 0, None)
    for item in items:
        if any(key not in item for key in contract.required_item_fields):
            return CollectionValidation("STRUCTURE_INVALID", len(items), None)

    observed_count = len(items)
    if contract.count_key is not None:
        if stated_count is None:
            return CollectionValidation("COUNT_UNKNOWN", None, None)
        if stated_count != len(items):
            return CollectionValidation("COUNT_MISMATCH", len(items), None)
        observed_count = stated_count
    if contract.expected_count is not None and observed_count != contract.expected_count:
        return CollectionValidation("COUNT_MISMATCH", observed_count, None)

    tail_matches = None
    if contract.expected_tail is not None:
        tail_matches = items[-1].get(contract.tail_key) == contract.expected_tail
        if not tail_matches:
            return CollectionValidation("TAIL_MISMATCH", observed_count, False)
    elif contract.tail_key is not None and contract.tail_key not in items[-1]:
        return CollectionValidation("STRUCTURE_INVALID", observed_count, None)
    return CollectionValidation("OUTPUT_VALID", observed_count, tail_matches)


__all__ = ["CollectionContract", "CollectionValidation", "validate_collection_output"]
