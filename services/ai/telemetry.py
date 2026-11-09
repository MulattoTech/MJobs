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
from datetime import datetime, timezone
import threading
import time
from typing import Generator, Literal, Mapping, TypedDict


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
}

def calculate_token_cost(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    *,
    provider: str | None = None,
) -> float | None:
    """Estimate only when both token counts and a matching price are known."""
    if prompt_tokens is None or completion_tokens is None:
        return None
    clean_model = (model or "").casefold().strip()
    clean_provider = (provider or "").casefold().strip()
    if clean_provider in {"ollama", "local"} or (not clean_provider and clean_model.startswith("ollama/")):
        pricing = (0.0, 0.0)
    else:
        # Deployment aliases and dated variants may have different prices.
        pricing = MODEL_PRICING_PER_MILLION.get(clean_model)
    if pricing is None:
        return None
    return round(
        max(0, prompt_tokens) * pricing[0] / 1_000_000
        + max(0, completion_tokens) * pricing[1] / 1_000_000,
        6,
    )


class AIBudgetExceededError(RuntimeError):
    """Raised when an AI task or session exceeds its configured budget ceiling."""

    def __init__(self, message: str, current_cost: float, budget_limit: float) -> None:
        super().__init__(message)
        self.current_cost = current_cost
        self.budget_limit = budget_limit


class AIAdmissionCancelledError(RuntimeError):
    """Raised when a queued request is cancelled before admission."""


class AIAdmissionObserverError(RuntimeError):
    """A cancellation observer failed without disclosing its private details."""

    def __init__(self) -> None:
        super().__init__("AI_ADMISSION_OBSERVER_UNAVAILABLE")


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
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    usage_provenance: Literal["PROVIDER_REPORTED", "PARTIAL", "UNAVAILABLE", "NOT_SENT"]
    cost_provenance: Literal["PRICE_TABLE_ESTIMATE", "LOCAL_ZERO", "UNAVAILABLE", "NOT_SENT"]
    request_sent: bool
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
    unknown_usage_calls: int
    unknown_cost_calls: int
    avg_latency_ms: float


class ProviderTelemetryReport(TypedDict):
    provider: str
    active_concurrency: int
    concurrency_limit: int
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    unknown_usage_calls: int
    unknown_cost_calls: int
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
    unknown_usage_calls: int
    unknown_cost_calls: int
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
        cancellation_event: threading.Event | None = None,
    ) -> Generator[None, None, None]:
        """Acquire a process-local slot subject to concurrency, budget and deadline gates.

        Caller-owned observers may perform I/O or inspect this engine. Never run
        them under the accounting lock, and recheck time after they return. This
        prevents late admission; it cannot preempt a blocked synchronous observer.
        """
        p_norm = provider.casefold().strip()
        deadline = time.monotonic() + timeout_seconds

        def check_cancellation() -> None:
            if cancellation_event is None:
                return
            try:
                cancelled = bool(cancellation_event.is_set())
            except Exception:
                raise AIAdmissionObserverError() from None
            if cancelled:
                raise AIAdmissionCancelledError()

        acquired = False
        try:
            check_cancellation()
            with self._lock:
                if self.budget_ceiling_usd is not None and self._total_cost_usd >= self.budget_ceiling_usd:
                    raise AIBudgetExceededError(
                        f"AI budget ceiling (${self.budget_ceiling_usd:.2f}) reached. Task '{task}' halted.",
                        current_cost=self._total_cost_usd,
                        budget_limit=self.budget_ceiling_usd,
                    )

            while time.monotonic() < deadline:
                check_cancellation()
                with self._lock:
                    # Both the observer and lock acquisition can consume the
                    # remaining wait. Check again before changing any counters.
                    if time.monotonic() >= deadline:
                        break
                    if self.budget_ceiling_usd is not None and self._total_cost_usd >= self.budget_ceiling_usd:
                        raise AIBudgetExceededError(
                            f"AI budget ceiling (${self.budget_ceiling_usd:.2f}) reached. Task '{task}' halted.",
                            current_cost=self._total_cost_usd,
                            budget_limit=self.budget_ceiling_usd,
                        )
                    provider_limit = self.provider_concurrency_limits.get(p_norm, self.global_concurrency_limit)
                    provider_active = self._active_by_provider.get(p_norm, 0)
                    if self._active_global < self.global_concurrency_limit and provider_active < provider_limit:
                        self._active_global += 1
                        self._active_by_provider[p_norm] = provider_active + 1
                        acquired = True

                if acquired:
                    # Reservation cleanup already covers observer exceptions,
                    # cancellation, deadline expiry, and the caller's body.
                    check_cancellation()
                    if time.monotonic() < deadline:
                        yield
                        return
                    break
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

            check_cancellation()
            with self._lock:
                cur_act = self._active_by_provider.get(p_norm, 0)
                lim = self.provider_concurrency_limits.get(p_norm, self.global_concurrency_limit)
                if self._active_global >= self.global_concurrency_limit:
                    cur_act, lim = self._active_global, self.global_concurrency_limit
            raise AIConcurrencyLimitError(
                f"Admission deadline reached for provider '{provider}' ({cur_act}/{lim}).",
                provider=provider,
                current_active=cur_act,
                limit=lim,
            )
        finally:
            if acquired:
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
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        request_sent: bool = True,
        fallback_reason: str | None = None,
        error_code: str | None = None,
    ) -> AttemptRecord:
        """Record an attempt with token, cost, and latency metrics without storing prompt PII."""
        p_tokens = (max(0, int(prompt_tokens)) if prompt_tokens is not None else None) if request_sent else 0
        c_tokens = (max(0, int(completion_tokens)) if completion_tokens is not None else None) if request_sent else 0
        tot_tokens = p_tokens + c_tokens if p_tokens is not None and c_tokens is not None else None
        cost = calculate_token_cost(model, p_tokens, c_tokens, provider=provider) if request_sent else 0.0
        usage_provenance = (
            "NOT_SENT" if not request_sent else "PROVIDER_REPORTED" if p_tokens is not None and c_tokens is not None
            else "UNAVAILABLE" if p_tokens is None and c_tokens is None else "PARTIAL"
        )
        cost_provenance = (
            "NOT_SENT" if not request_sent else "UNAVAILABLE" if cost is None else
            "LOCAL_ZERO" if provider.casefold().strip() in {"ollama", "local"}
            else "PRICE_TABLE_ESTIMATE"
        )

        record: AttemptRecord = {
            "attempt_id": "",
            "task": str(task),
            "provider": str(provider),
            "model": str(model),
            "latency_ms": round(max(0.0, float(latency_ms)), 2),
            "status": status,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "total_tokens": tot_tokens,
            "estimated_cost": cost,
            "usage_provenance": usage_provenance,
            "cost_provenance": cost_provenance,
            "request_sent": request_sent,
            "fallback_reason": fallback_reason,
            "error_code": error_code,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

        with self._lock:
            record["attempt_id"] = f"att-{len(self._attempts) + 1}-{int(time.time() * 1000)}"
            self._attempts.append(record)
            self._total_cost_usd = round(self._total_cost_usd + (cost or 0.0), 6)

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
        tot_prompt = sum(a["prompt_tokens"] or 0 for a in attempts)
        tot_comp = sum(a["completion_tokens"] or 0 for a in attempts)
        unknown_usage = sum(a["usage_provenance"] not in ("PROVIDER_REPORTED", "NOT_SENT") for a in attempts)
        unknown_cost = sum(a["estimated_cost"] is None for a in attempts)
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
                    "unknown_usage_calls": 0,
                    "unknown_cost_calls": 0,
                    "error_rate": 0.0,
                }
            rep = by_provider[p]
            rep["total_calls"] += 1
            rep["total_tokens"] += (a["prompt_tokens"] or 0) + (a["completion_tokens"] or 0)
            rep["total_cost_usd"] = round(rep["total_cost_usd"] + (a["estimated_cost"] or 0.0), 6)
            rep["unknown_usage_calls"] += a["usage_provenance"] not in ("PROVIDER_REPORTED", "NOT_SENT")
            rep["unknown_cost_calls"] += a["estimated_cost"] is None

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
                    "unknown_usage_calls": 0,
                    "unknown_cost_calls": 0,
                    "avg_latency_ms": 0.0,
                }
            ts = by_task[t]
            ts["total_calls"] += 1
            if a["status"] == "SUCCESS":
                ts["successful_calls"] += 1
            else:
                ts["failed_calls"] += 1
            ts["total_prompt_tokens"] += a["prompt_tokens"] or 0
            ts["total_completion_tokens"] += a["completion_tokens"] or 0
            ts["total_tokens"] += (a["prompt_tokens"] or 0) + (a["completion_tokens"] or 0)
            ts["total_cost_usd"] = round(ts["total_cost_usd"] + (a["estimated_cost"] or 0.0), 6)
            ts["unknown_usage_calls"] += a["usage_provenance"] not in ("PROVIDER_REPORTED", "NOT_SENT")
            ts["unknown_cost_calls"] += a["estimated_cost"] is None

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
            "unknown_usage_calls": unknown_usage,
            "unknown_cost_calls": unknown_cost,
            "budget_ceiling_usd": self.budget_ceiling_usd,
            "budget_remaining_usd": round(remaining_budget, 4) if remaining_budget is not None else None,
            "budget_warning_triggered": warning_triggered,
            "by_provider": by_provider,
            "by_task": by_task,
            "recent_attempts": attempts[-20:],
        }


# Global default telemetry engine instance
_DEFAULT_ENGINE = LLMTelemetryEngine(budget_ceiling_usd=None)


def get_default_telemetry_engine() -> LLMTelemetryEngine:
    return _DEFAULT_ENGINE
