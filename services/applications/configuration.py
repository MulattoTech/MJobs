"""Validated deployment settings for the existing submission admission policy."""

import os
import re

from packages.domain.safety import Policy


_LIMITS = {
    "hourly_limit": ("MJOBS_SUBMISSION_HOURLY_LIMIT", "3"),
    "daily_limit": ("MJOBS_SUBMISSION_DAILY_LIMIT", "10"),
    "company_limit": ("MJOBS_SUBMISSION_COMPANY_LIMIT", "1"),
}


def configured_submission_policy(environ=None) -> Policy:
    """Read bounded decimal caps; configuration cannot enable external submission."""
    environment = os.environ if environ is None else environ
    limits = {}
    for field, (name, default) in _LIMITS.items():
        value = environment.get(name, default)
        if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}", value)
                or int(value) > 10000):
            raise ValueError(f"{name}_INVALID")
        limits[field] = int(value)
    return Policy(external_submissions_enabled=False, **limits)
