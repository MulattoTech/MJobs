"""Integration tests for LLM Telemetry, Cost & Concurrency (MJ-REF-015)."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import unittest

from services.ai.tasks import AITask
from services.ai.telemetry import LLMTelemetryEngine


class LLMTelemetryIntegrationTests(unittest.TestCase):
    def test_multithreaded_concurrency_and_cost_accounting(self):
        engine = LLMTelemetryEngine(
            global_concurrency_limit=5,
            provider_concurrency_limits={"openai": 3, "ollama": 2},
            budget_ceiling_usd=20.0,
        )

        tasks = [
            (AITask.JOB_SCORING, "openai", "gpt-4o", 1200, 250),
            (AITask.REQUIREMENT_EXTRACTION, "openai", "gpt-4o-mini", 800, 150),
            (AITask.RESUME_TAILORING, "ollama", "ollama/llama3", 3000, 500),
            (AITask.COVER_LETTERS, "anthropic", "claude-3-5-sonnet", 2500, 600),
            (AITask.FREE_FORM_ANSWERS, "ollama", "ollama/mistral", 1500, 300),
            (AITask.COMPANY_SUMMARIZATION, "openai", "gpt-4o-mini", 900, 200),
        ] * 4  # 24 requests in total across threads

        def _worker(task_tuple, worker_id):
            task_type, provider, model, p_tokens, c_tokens = task_tuple
            t0 = time.monotonic()
            with engine.acquire_slot(provider, task=task_type.value, timeout_seconds=2.0):
                # Simulate small I/O delay
                time.sleep(0.005)
                latency = (time.monotonic() - t0) * 1000
                return engine.record_attempt(
                    task=task_type.value,
                    provider=provider,
                    model=model,
                    latency_ms=latency,
                    status="SUCCESS",
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                )

        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_worker, t, idx) for idx, t in enumerate(tasks)]
            for future in as_completed(futures):
                results.append(future.result())

        self.assertEqual(len(results), 24)

        summary = engine.get_summary()
        self.assertEqual(summary["total_calls"], 24)
        self.assertEqual(summary["successful_calls"], 24)
        self.assertEqual(summary["failed_calls"], 0)
        self.assertEqual(summary["active_concurrency"], 0)

        # Check that costs are strictly positive for commercial models and 0 for ollama
        self.assertGreater(summary["total_cost_usd"], 0.0)
        self.assertIn("ollama", summary["by_provider"])
        self.assertEqual(summary["by_provider"]["ollama"]["total_cost_usd"], 0.0)

        # Check task coverage
        self.assertIn(AITask.JOB_SCORING.value, summary["by_task"])
        self.assertIn(AITask.RESUME_TAILORING.value, summary["by_task"])

    def test_privacy_guarantee_no_pii_in_telemetry(self):
        engine = LLMTelemetryEngine()

        # Record attempt with no raw prompt
        att = engine.record_attempt(
            task="cover_letters",
            provider="openai",
            model="gpt-4o",
            latency_ms=300,
            status="SUCCESS",
            prompt_tokens=2000,
            completion_tokens=400,
        )

        # Invariant: prompt text or PII should never be stored in AttemptRecord
        self.assertNotIn("prompt", att)
        self.assertNotIn("prompt_text", att)
        self.assertNotIn("input_content", att)
        self.assertNotIn("pii", att)


if __name__ == "__main__":
    unittest.main()
