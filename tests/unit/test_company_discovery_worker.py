import os
import unittest
from unittest.mock import patch

from workers import main as shared_worker
from workers.operations import QUEUES


class CompanyDiscoveryWorkerTests(unittest.TestCase):
    def test_runner_always_consumes_only_the_company_discovery_queue(self):
        company_queue = QUEUES["COMPANY_DISCOVER"]
        try:
            from workers.company_discovery_worker import main
        except ImportError as error:
            self.fail(f"company-discovery worker entrypoint is missing: {error}")
        with patch.dict(os.environ, {
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_DATABASE_URL": "sqlite:///unused-company-worker.db",
            "MJOBS_REDIS_URL": "redis://unused:6379/0",
            "MJOBS_WORKER_QUEUE": "mjobs-application",
        }), patch.object(shared_worker, "Store") as store_type, \
                patch.object(shared_worker.Redis, "from_url") as redis_from_url, \
                patch.object(shared_worker, "Worker") as worker_type, \
                patch.object(shared_worker, "dispatch_pending"):
            main()
            self.assertEqual(os.environ["MJOBS_WORKER_QUEUE"], "mjobs-application")

        worker_type.assert_called_once_with([company_queue], connection=redis_from_url.return_value)
        store_type.return_value.bind_workspace_mode.assert_called_once_with("PERSONAL")
        redis_from_url.return_value.close.assert_called_once_with()
        store_type.return_value.engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
