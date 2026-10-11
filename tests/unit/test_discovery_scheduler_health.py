"""Scheduler health is refreshed only after a successful full scheduler tick."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from services.discovery.scheduler import scheduler_tick


class DiscoverySchedulerHealthTests(unittest.TestCase):
    def test_successful_tick_publishes_a_runtime_heartbeat(self):
        store = object()
        redis = object()
        at = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        result = {"scheduled": [], "skipped": []}

        with patch("services.discovery.scheduler.schedule_due_sources", return_value=result) as schedule, \
                patch("services.discovery.scheduler.publish_scheduler_heartbeat", return_value=at.isoformat()) as publish:
            observed = scheduler_tick(store, redis, at=at)

        self.assertIs(observed, result)
        schedule.assert_called_once_with(store, at)
        publish.assert_called_once_with(redis, at=at, interval_seconds=30)

    def test_tick_passes_the_configured_maximum_interval_to_heartbeat(self):
        at = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = object()
        with patch("services.discovery.scheduler.schedule_due_sources", return_value={}), \
                patch("services.discovery.scheduler.publish_scheduler_heartbeat") as publish:
            scheduler_tick(object(), redis, at=at, interval_seconds=300)

        publish.assert_called_once_with(redis, at=at, interval_seconds=300)

    def test_failed_tick_preserves_no_freshness_claim(self):
        with patch("services.discovery.scheduler.schedule_due_sources", side_effect=RuntimeError("tick failed")), \
                patch("services.discovery.scheduler.publish_scheduler_heartbeat") as publish:
            with self.assertRaisesRegex(RuntimeError, "tick failed"):
                scheduler_tick(object(), object())

        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
