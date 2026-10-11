import unittest
from unittest.mock import Mock

from workers.transient_retention import CONFIG_ENV
from services.discovery.scheduler import run_transient_retention_if_configured


class TransientRetentionSchedulerTests(unittest.TestCase):
    def test_missing_policy_is_a_noop_and_never_selects_a_root(self):
        runner = Mock()
        self.assertIsNone(run_transient_retention_if_configured({}, runner=runner))
        runner.assert_not_called()

    def test_explicit_validated_policy_runs_and_returns_only_worker_summary(self):
        config = '{"configured_root":"synthetic"}'
        result = {"status":"OK", "dry_run":False, "removed_count":2, "removed_bytes":50}
        runner = Mock(return_value=result)

        actual = run_transient_retention_if_configured({CONFIG_ENV:config}, runner=runner)

        runner.assert_called_once_with(config)
        self.assertEqual(actual, result)


if __name__ == '__main__':
    unittest.main()
