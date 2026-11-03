"""Bounded HTTP Retry-After interpretation for read-only source cooldowns."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re

# Existing collection/retry policy disables automatic collection above one year.
MAX_RETRY_SECONDS = 31_536_000
# Oversized input cannot be interpreted: trigger disabled/max-date backoff.
DISABLE_RETRY_SECONDS = 10 ** 12
_MAX_HEADER_LENGTH = 128
_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_IMF_DATE = re.compile(rf"{_DAY}, [0-9]{{2}} {_MONTH} [0-9]{{4}} {_TIME} GMT")
_RFC850_DATE = re.compile(
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    rf"[0-9]{{2}}-{_MONTH}-[0-9]{{2}} {_TIME} GMT")
_ASCTIME_DATE = re.compile(rf"{_DAY} {_MONTH} (?:[0-9]{{2}}| [0-9]) {_TIME} [0-9]{{4}}")


def retry_after_seconds(value, *, default=60, minimum=1, invalid_default=None, now=None):
    """Return a safe delay without retaining provider-controlled text.

    Missing/malformed headers keep the caller's existing fallback. Oversized
    headers return a disable/max-date sentinel. Valid long intervals are kept
    intact for the existing policy to disable; dates round upward.
    Policy arguments are trusted adapter constants; now is an aware test clock.
    """
    fallback = max(minimum, default if invalid_default is None else invalid_default)
    if value is None:
        return max(minimum, default)
    if not isinstance(value, str):
        return fallback
    if len(value) > _MAX_HEADER_LENGTH:
        return DISABLE_RETRY_SECONDS
    value = value.strip(" \t")
    if re.fullmatch(r"[0-9]+", value):
        return max(minimum, int(value))
    rfc850 = _RFC850_DATE.fullmatch(value)
    if not (_IMF_DATE.fullmatch(value) or rfc850 or _ASCTIME_DATE.fullmatch(value)):
        return fallback
    instant = now if now is not None else datetime.now(timezone.utc)
    try:
        if instant.tzinfo is None:
            return fallback
        instant = instant.astimezone(timezone.utc)
        leap_second = re.search(r"23:59:60(?= GMT$| [0-9]{4}$)", value)
        if leap_second:
            value = value[:leap_second.start()] + "23:59:59" + value[leap_second.end():]
        requested = parsedate_to_datetime(value)
        if requested.tzinfo is None:  # The accepted asctime form is UTC.
            requested = requested.replace(tzinfo=timezone.utc)
        if rfc850:
            # RFC9110 resolves two-digit years relative to the receipt clock:
            # dates appearing over fifty years ahead mean the previous century.
            year = ((instant.year + 50) // 100) * 100 + requested.year % 100
            candidate = (year, requested.month, requested.day, requested.hour,
                         requested.minute, requested.second, requested.microsecond)
            cutoff = (instant.year + 50, instant.month, instant.day, instant.hour,
                      instant.minute, instant.second, instant.microsecond)
            if candidate > cutoff:
                year -= 100
            requested = requested.replace(year=year)
        if leap_second:
            requested += timedelta(seconds=1)
        seconds = math.ceil((requested - instant).total_seconds())
    except OverflowError:
        return DISABLE_RETRY_SECONDS
    except (TypeError, ValueError):
        return fallback
    return max(minimum, seconds)
