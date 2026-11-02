"""Explicit, bounded saved-search automation authority.

The v1 wire format remains part of a saved search, independently of its search
criteria and source polling policy. Unknown actions/fields cannot grant work.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SavedSearchRule:
    enabled: bool
    interval_seconds: int

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Saved search scheduler enabled must be boolean")
        if type(self.interval_seconds) is not int or not 300 <= self.interval_seconds <= 604800:
            raise ValueError("Saved search scheduler interval must be an integer from 300 to 604800 seconds")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict) or set(value) != {"enabled", "interval_seconds"}:
            raise ValueError("Saved search scheduler must include enabled and interval_seconds")
        return cls(**value)

    def as_dict(self):
        return {"enabled": self.enabled, "interval_seconds": self.interval_seconds}
