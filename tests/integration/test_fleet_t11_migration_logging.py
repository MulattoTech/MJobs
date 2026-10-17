"""In-process schema upgrades retain privacy-bounded worker outcome logging."""
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config

from workers.structured_logging import log_operation_finished


class MigrationOperationLoggingTests(unittest.TestCase):
    def test_upgrade_preserves_existing_operation_logger_and_private_event(self):
        logger = logging.getLogger("mjobs.operations")
        with tempfile.TemporaryDirectory(prefix="mjobs-t11-migration-log-") as root:
            url = "sqlite:///" + (Path(root) / "synthetic.db").as_posix()
            # Restore the caller's enabled/disabled state even when upgrade fails.
            with patch.object(logger, "disabled", False), patch.dict(
                    os.environ, {"MJOBS_DATABASE_URL": url}):
                command.upgrade(Config("alembic.ini"), "head")
                self.assertFalse(logger.disabled)
                with self.assertLogs(logger, level="INFO") as captured:
                    log_operation_finished({
                        "id": "synthetic-operation", "kind": "RESUME_EXTRACT_QUEUED",
                        "params": {"resume_text": "PRIVATE_SYNTHETIC_RESUME"},
                        "log_context": {"profile_id": "PRIVATE_SYNTHETIC_PROFILE"},
                    }, "COMPLETED", None, 0.01)
            self.assertEqual(len(captured.records), 1)
            encoded = captured.records[0].getMessage()
            event = json.loads(encoded)
            self.assertEqual(event["operation_id"], "synthetic-operation")
            self.assertEqual(event["operation_kind"], "RESUME_EXTRACT_QUEUED")
            self.assertEqual(event["event"], "operation_finished")
            self.assertEqual(event["result"], "COMPLETED")
            self.assertNotIn("PRIVATE_SYNTHETIC", encoded)
            self.assertNotIn(root, encoded)
