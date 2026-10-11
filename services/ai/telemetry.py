"""LLM Token, Cost, Provider-Attempt, and Concurrency Telemetry (MJ-REF-015).

Acceptance criteria:
1. Record provider/model, attempts/fallbacks, latency, prompt/output token counts,
   and estimated/actual cost provenance.
2. Add global and per-provider concurrency/rate caps.
3. Expose aggregate cost/usage analytics without logging prompt PII.
4. Allow configurable budget warnings/stops by task/provider.
5. Thread-safe operations, fallback chains, missing metadata handling.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
import threading
import time
from typing import Any, Generator, Literal, TypedDict


MODEL_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    # model: (prompt_cost_per_million_tokens, completion_cost_per_million_tokens)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Local & self-hosted execution has zero incremental API cost
    "ollama": (0.00, 0.00),
    "local": (0.00, 0.00),
}

DEFAULT_PRICING: tuple[float, float] = (1.00, 3.00)


def calculate_token_cost(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Calculate estimated cost in USD with model price tables."""
    clean_model = (model or "").casefold().strip()

    pricing = DEFAULT_PRICING
    if clean_model.startswith("ollama") or "local" in clean_model:
        pricing = (0.0, 0.0)
    else:
        for known_model, price in MODEL_PRICING_PER_MILLION.items():
            if known_model in clean_model:
                pricing = price
                break

    prompt_cost = (max(0, prompt_tokens) / 1_000_000.0) * pricing[0]
    completion_cost = (max(0, completion_tokens) / 1_000_000.0) * pricing[1]
    return round(prompt_cost + completion_cost, 6)


class AIBudgetExceededError(RuntimeError):
    """Raised when an AI task or session exceeds its configured budget ceiling."""

    def __init__(self, message: str, current_cost: float, budget_limit: float) -> None:
        super().__init__(message)
        self.current_cost = current_cost
        self.budget_limit = budget_limit


class AIConcurrencyLimitError(RuntimeError):
    """Raised when concurrency limits for a provider or global session are saturated."""

    def __init__(self, message: str, provider: str, current_active: int, limit: int) -> None:
        super().__init__(message)
        self.provider = provider
        self.current_active = current_active
        self.limit = limit


class AttemptRecord(TypedDict):
    attempt_id: str
    task: str
    provider: str
    model: str
    latency_ms: float
    status: Literal["SUCCESS", "FAILED", "RATE_LIMITED", "FALLBACK", "BUDGET_STOP"]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    fallback_reason: str | None
    error_code: str | None
    timestamp_utc: str


class TaskUsageSummary(TypedDict):
    total_calls: int
    successful_calls: int
    failed_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_usd: float
    avg_latency_ms: float


class ProviderTelemetryReport(TypedDict):
    provider: str
    active_concurrency: int
    concurrency_limit: int
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    error_rate: float


class AggregateTelemetryReport(TypedDict):
    total_calls: int
    successful_calls: int
    failed_calls: int
    active_concurrency: int
    global_concurrency_limit: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_usd: float
    budget_ceiling_usd: float | None
    budget_remaining_usd: float | None
    budget_warning_triggered: bool
    by_provider: dict[str, ProviderTelemetryReport]
    by_task: dict[str, TaskUsageSummary]
    recent_attempts: list[AttemptRecord]


class LLMTelemetryEngine:
    """Thread-safe telemetry engine tracking attempts, tokens, costs, and concurrency."""

    def __init__(
        self,
        *,
        global_concurrency_limit: int = 8,
        provider_concurrency_limits: Mapping[str, int] | None = None,
        budget_ceiling_usd: float | None = 10.0,
        budget_warning_threshold: float = 0.8,
    ) -> None:
        self.global_concurrency_limit = max(1, int(global_concurrency_limit))
        self.provider_concurrency_limits = {
            k.casefold(): max(1, int(v)) for k, v in (provider_concurrency_limits or {}).items()
        }
        self.budget_ceiling_usd = budget_ceiling_usd
        self.budget_warning_threshold = budget_warning_threshold

        self._lock = threading.Lock()
        self._active_global: int = 0
        self._active_by_provider: dict[str, int] = {}
        self._attempts: list[AttemptRecord] = []
        self._total_cost_usd: float = 0.0

    @contextmanager
    def acquire_slot(
        self,
        provider: str,
        task: str = "general",
        *,
        timeout_seconds: float = 5.0,
    ) -> Generator[None, None, None]:
        """Acquire a concurrency slot subject to global and per-provider rate ceilings."""
        p_norm = provider.casefold().strip()
        deadline = time.monotonic() + timeout_seconds

        with self._lock:
            # Check budget stop before admitting request
            if self.budget_ceiling_usd is not None and self._total_cost_usd >= self.budget_ceiling_usd:
                raise AIBudgetExceededError(
                    f"AI budget ceiling (${self.budget_ceiling_usd:.2f}) reached. Task '{task}' halted.",
                    current_cost=self._total_cost_usd,
                    budget_limit=self.budget_ceiling_usd,
                )

        acquired = False
        while time.monotonic() < deadline:
            with self._lock:
                provider_limit = self.provider_concurrency_limits.get(p_norm, self.global_concurrency_limit)
                provider_active = self._active_by_provider.get(p_norm, 0)

                if (self._active_global < self.global_concurrency_limit) and (provider_active < provider_limit):
                    self._active_global += 1
                    self._active_by_provider[p_norm] = provider_active + 1
                    acquired = True
                    break

            time.sleep(0.01)

        if not acquired:
            with self._lock:
                cur_act = self._active_by_provider.get(p_norm, 0)
                lim = self.provider_concurrency_limits.get(p_norm, self.global_concurrency_limit)
            raise AIConcurrencyLimitError(
                f"Concurrency limit saturated for provider '{provider}' ({cur_act}/{lim}).",
                provider=provider,
                current_active=cur_act,
                limit=lim,
            )

        try:
            yield
        finally:
            with self._lock:
                self._active_global = max(0, self._active_global - 1)
                self._active_by_provider[p_norm] = max(0, self._active_by_provider.get(p_norm, 1) - 1)

    def record_attempt(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        latency_ms: float,
        status: Literal["SUCCESS", "FAILED", "RATE_LIMITED", "FALLBACK", "BUDGET_STOP"],
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        fallback_reason: str | None = None,
        error_code: str | None = None,
    ) -> AttemptRecord:
        """Record an attempt with token, cost, and latency metrics without storing prompt PII."""
        p_tokens = max(0, int(prompt_tokens))
        c_tokens = max(0, int(completion_tokens))
        tot_tokens = p_tokens + c_tokens
        cost = calculate_token_cost(model, p_tokens, c_tokens)

        record: AttemptRecord = {
            "attempt_id": f"att-{len(self._attempts) + 1}-{int(time.time() * 1000)}",
            "task": str(task),
            "provider": str(provider),
            "model": str(model),
            "latency_ms": round(max(0.0, float(latency_ms)), 2),
            "status": status,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "total_tokens": tot_tokens,
            "estimated_cost": cost,
            "fallback_reason": fallback_reason,
            "error_code": error_code,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

        with self._lock:
            self._attempts.append(record)
            self._total_cost_usd = round(self._total_cost_usd + cost, 6)

        return record

    def get_summary(self) -> AggregateTelemetryReport:
        """Generate privacy-preserving aggregate telemetry report."""
        with self._lock:
            attempts = list(self._attempts)
            active_glob = self._active_global
            active_by_p = dict(self._active_by_provider)
            total_cost = self._total_cost_usd

        total_calls = len(attempts)
        successful = sum(1 for a in attempts if a["status"] == "SUCCESS")
        failed = sum(1 for a in attempts if a["status"] in ("FAILED", "RATE_LIMITED", "BUDGET_STOP"))
        tot_prompt = sum(a["prompt_tokens"] for a in attempts)
        tot_comp = sum(a["completion_tokens"] for a in attempts)
        tot_tokens = tot_prompt + tot_comp

        warning_triggered = False
        remaining_budget = None
        if self.budget_ceiling_usd is not None:
            remaining_budget = max(0.0, self.budget_ceiling_usd - total_cost)
            if total_cost >= (self.budget_ceiling_usd * self.budget_warning_threshold):
                warning_triggered = True

        # Group by provider
        by_provider: dict[str, ProviderTelemetryReport] = {}
        for a in attempts:
            p = a["provider"]
            if p not in by_provider:
                lim = self.provider_concurrency_limits.get(p.casefold(), self.global_concurrency_limit)
                by_provider[p] = {
                    "provider": p,
                    "active_concurrency": active_by_p.get(p.casefold(), 0),
                    "concurrency_limit": lim,
                    "total_calls": 0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "error_rate": 0.0,
                }
            rep = by_provider[p]
            rep["total_calls"] += 1
            rep["total_tokens"] += a["total_tokens"]
            rep["total_cost_usd"] = round(rep["total_cost_usd"] + a["estimated_cost"], 6)

        for p, rep in by_provider.items():
            p_attempts = [a for a in attempts if a["provider"] == p]
            p_failed = sum(1 for a in p_attempts if a["status"] != "SUCCESS")
            rep["error_rate"] = round(p_failed / len(p_attempts), 3) if p_attempts else 0.0

        # Group by task
        by_task: dict[str, TaskUsageSummary] = {}
        for a in attempts:
            t = a["task"]
            if t not in by_task:
                by_task[t] = {
                    "total_calls": 0,
                    "successful_calls": 0,
                    "failed_calls": 0,
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "avg_latency_ms": 0.0,
                }
            ts = by_task[t]
            ts["total_calls"] += 1
            if a["status"] == "SUCCESS":
                ts["successful_calls"] += 1
            else:
                ts["failed_calls"] += 1
            ts["total_prompt_tokens"] += a["prompt_tokens"]
            ts["total_completion_tokens"] += a["completion_tokens"]
            ts["total_tokens"] += a["total_tokens"]
            ts["total_cost_usd"] = round(ts["total_cost_usd"] + a["estimated_cost"], 6)

        for t, ts in by_task.items():
            t_attempts = [a for a in attempts if a["task"] == t]
            ts["avg_latency_ms"] = (
                round(sum(a["latency_ms"] for a in t_attempts) / len(t_attempts), 1)
                if t_attempts
                else 0.0
            )

        return {
            "total_calls": total_calls,
            "successful_calls": successful,
            "failed_calls": failed,
            "active_concurrency": active_glob,
            "global_concurrency_limit": self.global_concurrency_limit,
            "total_prompt_tokens": tot_prompt,
            "total_completion_tokens": tot_comp,
            "total_tokens": tot_tokens,
            "total_cost_usd": round(total_cost, 6),
            "budget_ceiling_usd": self.budget_ceiling_usd,
            "budget_remaining_usd": round(remaining_budget, 4) if remaining_budget is not None else None,
            "budget_warning_triggered": warning_triggered,
            "by_provider": by_provider,
            "by_task": by_task,
            "recent_attempts": attempts[-20:],
        }


# Global default telemetry engine instance
_DEFAULT_ENGINE = LLMTelemetryEngine()


def get_default_telemetry_engine() -> LLMTelemetryEngine:
    return _DEFAULT_ENGINE
