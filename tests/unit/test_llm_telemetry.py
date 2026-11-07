"""Unit tests for LLM Telemetry, Cost & Concurrency (MJ-REF-015)."""
import threading
import unittest

from services.ai.telemetry import (
    AIBudgetExceededError,
    AIConcurrencyLimitError,
    LLMTelemetryEngine,
    calculate_token_cost,
    get_default_telemetry_engine,
)


class LLMTelemetryUnitTests(unittest.TestCase):
    def test_calculate_token_cost_pricing_models(self):
        # 1. GPT-4o: $2.50/M prompt, $10.00/M completion
        cost_gpt4o = calculate_token_cost("gpt-4o", 10_000, 2_000)
        # 10k * 2.50 / 1M = 0.025, 2k * 10 / 1M = 0.02 -> 0.045
        self.assertAlmostEqual(cost_gpt4o, 0.045, places=4)

        # 2. Claude-3-5-Sonnet: $3.00/M prompt, $15.00/M completion
        cost_claude = calculate_token_cost("claude-3-5-sonnet", 10_000, 1_000)
        # 10k * 3 / 1M = 0.03, 1k * 15 / 1M = 0.015 -> 0.045
        self.assertAlmostEqual(cost_claude, 0.045, places=4)

        # 3. Ollama / Local: $0.00 always
        cost_ollama = calculate_token_cost("ollama/llama3", 50_000, 20_000)
        self.assertEqual(cost_ollama, 0.0)
        self.assertEqual(calculate_token_cost("custom-model", 50_000, 20_000, provider="local"), 0.0)
        self.assertIsNone(calculate_token_cost("ollama/llama3", 50_000, 20_000, provider="openai-compatible"))

        # Unknown pricing cannot support a cost estimate.
        self.assertIsNone(calculate_token_cost("custom-model", 10_000, 1_000))
        self.assertAlmostEqual(calculate_token_cost("gpt-4o-mini", 10_000, 1_000), 0.0021)
        self.assertIsNone(calculate_token_cost("gpt-4o", None, 1_000))

    def test_record_attempt_and_fallback_chain(self):
        engine = LLMTelemetryEngine(budget_ceiling_usd=10.0)

        # Attempt 1: primary provider rate limited
        att1 = engine.record_attempt(
            task="job_scoring",
            provider="openai",
            model="gpt-4o",
            latency_ms=120.5,
            status="RATE_LIMITED",
            prompt_tokens=1500,
            completion_tokens=0,
            error_code="RATE_LIMIT_429",
            fallback_reason="HTTP 429 received from OpenAI; falling back to Claude",
        )

        self.assertEqual(att1["status"], "RATE_LIMITED")
        self.assertEqual(att1["error_code"], "RATE_LIMIT_429")
        self.assertIn("falling back to Claude", att1["fallback_reason"])

        # Attempt 2: fallback provider succeeded
        att2 = engine.record_attempt(
            task="job_scoring",
            provider="anthropic",
            model="claude-3-5-sonnet",
            latency_ms=450.2,
            status="SUCCESS",
            prompt_tokens=1500,
            completion_tokens=300,
        )

        self.assertEqual(att2["status"], "SUCCESS")
        self.assertGreater(att2["estimated_cost"], 0.0)

        # Verify summary reflects both attempts
        summary = engine.get_summary()
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["successful_calls"], 1)
        self.assertEqual(summary["failed_calls"], 1)
        self.assertEqual(summary["total_tokens"], 3300)

        # Check by_provider grouping
        self.assertIn("openai", summary["by_provider"])
        self.assertIn("anthropic", summary["by_provider"])
        self.assertEqual(summary["by_provider"]["openai"]["error_rate"], 1.0)
        self.assertEqual(summary["by_provider"]["anthropic"]["error_rate"], 0.0)

    def test_concurrency_limits_enforced(self):
        engine = LLMTelemetryEngine(
            global_concurrency_limit=2,
            provider_concurrency_limits={"ollama": 1},
        )

        # 1. Test per-provider limit (ollama cap = 1)
        with engine.acquire_slot("ollama", "task1"):
            # Second attempt on ollama must fail with timeout/saturation
            with self.assertRaises(AIConcurrencyLimitError):
                with engine.acquire_slot("ollama", "task2", timeout_seconds=0.05):
                    pass

        # 2. Test slot release enables subsequent acquisition
        with engine.acquire_slot("ollama", "task3"):
            summary = engine.get_summary()
            self.assertEqual(summary["active_concurrency"], 1)

    def test_global_cap_blocks_another_provider_while_active(self):
        engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
        entered = threading.Event()
        release = threading.Event()

        def hold_slot():
            with engine.acquire_slot("openai", timeout_seconds=1):
                entered.set()
                release.wait(1)

        worker = threading.Thread(target=hold_slot)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(AIConcurrencyLimitError) as error:
                with engine.acquire_slot("azure-openai", timeout_seconds=0.03):
                    pass
            self.assertEqual(error.exception.current_active, 1)
            self.assertEqual(error.exception.limit, 1)
        finally:
            release.set()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(engine.get_summary()["active_concurrency"], 0)

    def test_missing_usage_and_unknown_price_are_explicit(self):
        engine = LLMTelemetryEngine(budget_ceiling_usd=None)
        missing = engine.record_attempt(
            task="scoring", provider="openai", model="gpt-4o",
            latency_ms=2, status="SUCCESS",
        )
        self.assertIsNone(missing["prompt_tokens"])
        self.assertIsNone(missing["estimated_cost"])
        self.assertEqual(missing["usage_provenance"], "UNAVAILABLE")
        partial = engine.record_attempt(
            task="scoring", provider="openai", model="gpt-4o",
            latency_ms=2, status="SUCCESS", prompt_tokens=10,
        )
        self.assertEqual(partial["usage_provenance"], "PARTIAL")
        self.assertIsNone(partial["total_tokens"])
        unknown_model = engine.record_attempt(
            task="scoring", provider="openai", model="deployment-alias",
            latency_ms=2, status="SUCCESS", prompt_tokens=10, completion_tokens=5,
        )
        self.assertEqual(unknown_model["usage_provenance"], "PROVIDER_REPORTED")
        self.assertEqual(unknown_model["cost_provenance"], "UNAVAILABLE")
        self.assertIsNone(unknown_model["estimated_cost"])
        summary = engine.get_summary()
        self.assertEqual(summary["unknown_usage_calls"], 2)
        self.assertEqual(summary["unknown_cost_calls"], 3)
        self.assertEqual(summary["total_prompt_tokens"], 20)
        self.assertEqual(summary["total_tokens"], 25)  # Known lower bound includes partial prompt usage.
        self.assertEqual(summary["by_provider"]["openai"]["total_tokens"], 25)
        self.assertEqual(summary["by_task"]["scoring"]["total_tokens"], 25)

    def test_default_engine_has_no_implicit_budget_stop(self):
        self.assertIsNone(get_default_telemetry_engine().get_summary()["budget_ceiling_usd"])

    def test_not_sent_attempt_has_known_zero_usage_and_cost(self):
        engine = LLMTelemetryEngine(budget_ceiling_usd=None)
        attempt = engine.record_attempt(
            task="scoring", provider="openai", model="unknown-deployment",
            latency_ms=1, status="BUDGET_STOP", request_sent=False,
        )
        self.assertFalse(attempt["request_sent"])
        self.assertEqual(attempt["usage_provenance"], "NOT_SENT")
        self.assertEqual(attempt["cost_provenance"], "NOT_SENT")
        self.assertEqual(attempt["total_tokens"], 0)
        self.assertEqual(attempt["estimated_cost"], 0.0)
        summary = engine.get_summary()
        self.assertEqual(summary["unknown_usage_calls"], 0)
        self.assertEqual(summary["unknown_cost_calls"], 0)

    def test_budget_ceiling_and_warning_trigger(self):
        # Set a low budget of $0.05
        engine = LLMTelemetryEngine(
            budget_ceiling_usd=0.05,
            budget_warning_threshold=0.8,
        )

        # Record a call costing $0.042 (84% of budget)
        engine.record_attempt(
            task="resume_tailoring",
            provider="openai",
            model="gpt-4o",
            latency_ms=800,
            status="SUCCESS",
            prompt_tokens=12_000,
            completion_tokens=1_200,
        )

        summary = engine.get_summary()
        self.assertTrue(summary["budget_warning_triggered"])
        self.assertLess(summary["budget_remaining_usd"], 0.01)

        # Push past ceiling
        engine.record_attempt(
            task="resume_tailoring",
            provider="openai",
            model="gpt-4o",
            latency_ms=500,
            status="SUCCESS",
            prompt_tokens=10_000,
            completion_tokens=1_000,
        )

        # Next acquisition must be blocked by budget ceiling
        with self.assertRaises(AIBudgetExceededError):
            with engine.acquire_slot("openai", "resume_tailoring"):
                pass


if __name__ == "__main__":
    unittest.main()
