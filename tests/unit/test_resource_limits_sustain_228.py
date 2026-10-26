"""Synthetic proof that test/runtime work cannot expand without explicit bounds."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from apps.api.main import AICostPolicyInput
from packages.persistence.store import Store
from services.ai.consumer import AIJobEvaluation, AIJobInput, consume_ai_candidates
from services.ai.cost_controls import AICostPolicy
from workers.dispatch import enqueue_operation
from workers.operations import (
    OperationConcurrencyLimit, admit_inline, category_limit, claim, limit,
)


class _CountingRouter:
    def __init__(self) -> None:
        self.calls = []

    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        return AIJobEvaluation(category="REVIEW", rationale="Synthetic bounded result.")


def test_two_browser_reservations_bound_hundreds_of_synthetic_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission is the execution boundary; it does not launch Chromium."""

    store = Store(f"sqlite:///{(tmp_path / 'browser-limits.db').as_posix()}")
    store.migrate()
    browser_launch = Mock(side_effect=AssertionError("a real browser must not launch"))
    monkeypatch.setattr("services.applications.manual_auth.run_browser", browser_launch)
    monkeypatch.setenv("MJOBS_APPLICATION_AUTOMATION_CONCURRENCY", "2")

    admitted = []
    rejected = 0
    try:
        for index in range(200):
            try:
                admitted.append(admit_inline(store, "MANUAL_AUTH", f"synthetic-app-{index}"))
            except OperationConcurrencyLimit:
                rejected += 1
    finally:
        store.engine.dispose()

    assert len(admitted) == 2
    assert rejected == 198
    browser_launch.assert_not_called()


@pytest.mark.parametrize("configured", ["0", "3", "200", "many", ""])
def test_invalid_or_oversized_browser_worker_limit_fails_before_worker_start(
    configured: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker startup validates every category before RQ can consume work."""

    from workers import main as worker_main

    worker_started = Mock(side_effect=AssertionError("RQ worker must not start"))
    fake_store = SimpleNamespace(
        bind_workspace_mode=lambda _mode: None,
        engine=SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(worker_main, "Store", lambda _url: fake_store)
    monkeypatch.setattr(worker_main, "Worker", worker_started)
    monkeypatch.setenv("MJOBS_DATABASE_URL", "synthetic://not-opened")
    monkeypatch.setenv("MJOBS_REDIS_URL", "redis://synthetic.invalid/0")
    monkeypatch.setenv("MJOBS_OPERATION_MODE", "PERSONAL")
    monkeypatch.setenv("MJOBS_APPLICATION_AUTOMATION_CONCURRENCY", configured)

    with pytest.raises(ValueError, match="Operation concurrency outside conservative bounds"):
        worker_main.main()

    worker_started.assert_not_called()


def test_runtime_worker_categories_have_small_independent_maxima(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = {
        "MJOBS_SOURCE_COLLECTION_CONCURRENCY": "4",
        "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "2",
        "MJOBS_AI_CONCURRENCY": "2",
        "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "2",
        "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "2",
    }
    monkeypatch.setenv("MJOBS_SOURCE_COLLECTION_CONCURRENCY", "4")
    monkeypatch.setenv("MJOBS_COMPANY_DISCOVERY_CONCURRENCY", "2")
    monkeypatch.setenv("MJOBS_AI_CONCURRENCY", "2")
    monkeypatch.setenv("MJOBS_DOCUMENT_GENERATION_CONCURRENCY", "2")
    monkeypatch.setenv("MJOBS_APPLICATION_AUTOMATION_CONCURRENCY", "2")

    assert {name: category_limit(name) for name in (
        "source_collection", "company_discovery", "ai",
        "document_generation", "application_automation",
    )} == {
        "source_collection": 4,
        "company_discovery": 2,
        "ai": 2,
        "document_generation": 2,
        "application_automation": 2,
    }
    assert limit("APPLY_MOCK") == 2
    assert limit("AI_EVALUATE_QUEUED") == 2
    assert all(int(value) <= 4 for value in configured.values())


def test_durable_claim_gate_bounds_two_hundred_queued_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue depth is harmless: only the configured number may start work."""

    store = Store(f"sqlite:///{(tmp_path / 'worker-limits.db').as_posix()}")
    store.migrate()
    monkeypatch.setenv("MJOBS_SOURCE_COLLECTION_CONCURRENCY", "4")
    try:
        operation_ids = [
            enqueue_operation(store, "DISCOVER", f"synthetic-profile-{index}", {})
            for index in range(200)
        ]
        claimed = [claim(store, operation_id) for operation_id in operation_ids]
    finally:
        store.engine.dispose()

    assert sum(operation is not None for operation in claimed) == 4
    assert sum(operation is None for operation in claimed) == 196


def test_ai_execution_caps_two_hundred_candidates_to_three_fake_calls() -> None:
    router = _CountingRouter()
    candidates = [
        AIJobInput(
            f"job-{index:03}", 100 - (index % 5), "Synthetic role",
            "Example Systems", "Synthetic public description.",
        )
        for index in range(200)
    ]

    result = consume_ai_candidates(
        candidates,
        AICostPolicy(
            minimum_score=0,
            max_input_candidates=200,
            max_ai_candidates=3,
            max_document_candidates=0,
        ),
        router,
    )

    assert len(result.evaluations) == 3
    assert len(router.calls) == 3
    assert result.document_candidate_ids == ()


@pytest.mark.parametrize("overrides", [
    {"max_ai_candidates": 76},
    {"max_ai_candidates": 10_000},
    {"max_ai_candidates": -1},
    {"max_ai_candidates": 1, "max_document_candidates": 2},
])
def test_ai_cost_policy_model_rejects_invalid_or_oversized_limits(overrides) -> None:
    values = {
        "minimum_score": 75,
        "max_input_candidates": 5_000,
        "max_ai_candidates": 75,
        "max_document_candidates": 20,
        **overrides,
    }

    with pytest.raises(ValidationError):
        AICostPolicyInput.model_validate(values)
