"""Provider-neutral, bounded public notification data; no raw operation payloads."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class NotificationError(ValueError):
    """A stable diagnostic code safe for CLI output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    operation_id: str
    operation_kind: str
    state: str
    updated_at: str
    attempt_count: int
    message: str


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    code: str
    message_id: str | None = None
    retry_after: int = 0


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    execution_surface: str
    events: tuple[str, ...]
    authentication: tuple[str, ...]
    guarantees: tuple[str, ...]
    unsupported: tuple[str, ...]


class NotificationProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def send(self, event: NotificationEvent) -> DeliveryResult: ...
