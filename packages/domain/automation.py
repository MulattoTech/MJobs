"""Explicit, bounded saved-search automation authority.

The v1 wire format remains part of a saved search, independently of its search
criteria and source polling policy. Unknown actions/fields cannot grant work.
"""
from dataclasses import dataclass
from datetime import time
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_LOCAL_TIME = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")


@dataclass(frozen=True)
class SavedSearchWindow:
    """A local-wall-clock eligibility window; cadence is enforced separately."""
    time_zone: str
    start_local: str
    end_local: str

    def __post_init__(self):
        if (not isinstance(self.time_zone, str) or not self.time_zone
                or len(self.time_zone) > 128 or any(ord(char) < 32 for char in self.time_zone)):
            raise ValueError("Saved search scheduler window time_zone must be a bounded IANA key")
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Saved search scheduler window time_zone must be an available IANA key") from None
        for field_name, value in (("start_local", self.start_local), ("end_local", self.end_local)):
            if not isinstance(value, str) or not _LOCAL_TIME.fullmatch(value):
                raise ValueError(f"Saved search scheduler window {field_name} must use HH:MM")
            hour, minute = (int(part) for part in value.split(":"))
            if hour > 23:
                raise ValueError(f"Saved search scheduler window {field_name} must use HH:MM")
        if self.start_local == self.end_local:
            raise ValueError("Saved search scheduler window start and end must differ")

    @classmethod
    def from_mapping(cls, value):
        if (not isinstance(value, dict)
                or set(value) != {"time_zone", "start_local", "end_local"}):
            raise ValueError("Saved search scheduler window must include time_zone, start_local, and end_local")
        return cls(**value)

    def includes(self, instant):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Saved search scheduler time must be timezone-aware")
        local_time = instant.astimezone(ZoneInfo(self.time_zone)).time().replace(tzinfo=None)
        start = time.fromisoformat(self.start_local)
        end = time.fromisoformat(self.end_local)
        if start < end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    def as_dict(self):
        return {"time_zone": self.time_zone,
                "start_local": self.start_local,
                "end_local": self.end_local}


@dataclass(frozen=True)
class SavedSearchRule:
    """Enabled discovery-only cadence with an optional local-time eligibility window."""
    enabled: bool
    interval_seconds: int
    window: SavedSearchWindow | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Saved search scheduler enabled must be boolean")
        if type(self.interval_seconds) is not int or not 300 <= self.interval_seconds <= 604800:
            raise ValueError("Saved search scheduler interval must be an integer from 300 to 604800 seconds")
        if self.window is not None and not isinstance(self.window, SavedSearchWindow):
            raise ValueError("Saved search scheduler window is invalid")

    @classmethod
    def from_mapping(cls, value):
        required = {"enabled", "interval_seconds"}
        if (not isinstance(value, dict) or not required <= set(value)
                or set(value) - required - {"window"}):
            raise ValueError("Saved search scheduler must include enabled and interval_seconds")
        window = SavedSearchWindow.from_mapping(value["window"]) if "window" in value else None
        return cls(value["enabled"], value["interval_seconds"], window)

    def as_dict(self):
        result = {"enabled": self.enabled, "interval_seconds": self.interval_seconds}
        if self.window is not None:
            result["window"] = self.window.as_dict()
        return result
