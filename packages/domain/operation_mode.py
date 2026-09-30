"""Keep fictional verification actions separate from personal operation."""
from __future__ import annotations

import os


def parse_operation_mode(value: object) -> str:
    if value not in ("FIXTURE", "PERSONAL") or type(value) is not str:
        raise ValueError("MJOBS_OPERATION_MODE must be FIXTURE or PERSONAL")
    return value


def operation_mode_from_env() -> str:
    return parse_operation_mode(os.environ.get("MJOBS_OPERATION_MODE", "PERSONAL"))
