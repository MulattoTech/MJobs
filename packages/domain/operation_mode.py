"""Keep fictional verification actions separate from personal operation."""
from __future__ import annotations

import os


class WorkspaceModeError(RuntimeError):
    """The durable database identity does not authorize this operation mode."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def parse_operation_mode(value: object) -> str:
    if value not in ("FIXTURE", "PERSONAL") or type(value) is not str:
        raise ValueError("MJOBS_OPERATION_MODE must be FIXTURE or PERSONAL")
    return value


def operation_mode_from_env() -> str:
    return parse_operation_mode(os.environ.get("MJOBS_OPERATION_MODE", "PERSONAL"))
