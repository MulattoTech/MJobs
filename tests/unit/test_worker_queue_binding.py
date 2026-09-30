"""A browser worker cannot consume discovery or document jobs."""
import os
import unittest
from unittest.mock import patch

from workers import main as worker_main
from workers.operations import QUEUES


class WorkerQueueBindingTests(unittest.TestCase):
    def test_each_worker_consumes_only_its_configured_queue(self):
        for queue in QUEUES.values():
            with self.subTest(queue=queue), patch.dict(os.environ, {
                "MJOBS_OPERATION_MODE": "PERSONAL",
                "MJOBS_DATABASE_URL": "sqlite:///unused-worker-binding.db",
                "MJOBS_REDIS_URL": "redis://unused:6379/0",
                "MJOBS_WORKER_QUEUE": queue,
            }), patch.object(worker_main, "Store") as store_type, \
                    patch.object(worker_main.Redis, "from_url") as redis_from_url, \
                    patch.object(worker_main, "Worker") as worker_type, \
                    patch.object(worker_main, "dispatch_pending"):
                worker_main.main()
                store_type.return_value.bind_workspace_mode.assert_called_once_with("PERSONAL")
                worker_type.assert_called_once_with([queue], connection=redis_from_url.return_value)
                redis_from_url.return_value.close.assert_called_once_with()
                store_type.return_value.engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
