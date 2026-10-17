"""T11 lease configuration rejects unbounded worker execution windows."""
import unittest
from unittest.mock import patch

from workers.operations import timeout


class FleetT11LeaseTests(unittest.TestCase):
    def test_timeout_accepts_inclusive_safe_bounds(self):
        for value in ("30", "180", "600"):
            with self.subTest(value=value), patch.dict("os.environ", MJOBS_OPERATION_TIMEOUT=value):
                self.assertEqual(timeout(), int(value))

    def test_timeout_rejects_malformed_or_unbounded_values(self):
        for value in ("29", "601", "-1", "unlimited"):
            with self.subTest(value=value), patch.dict("os.environ", MJOBS_OPERATION_TIMEOUT=value):
                with self.assertRaises(ValueError):
                    timeout()


if __name__ == "__main__":
    unittest.main()
