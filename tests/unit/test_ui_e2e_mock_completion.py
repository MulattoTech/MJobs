import unittest

from cli.ui_e2e import wait_for_mock_run


class MockCompletionTests(unittest.TestCase):
    def wait(self, states, timeout=20):
        elapsed = [0.0]
        records = iter(states)
        last = [None]

        def read(path):
            self.assertEqual(path, "/owned-run")
            last[0] = next(records, last[0])
            return {"operation_id": "owned", "state": last[0],
                    "error_code": "private diagnostic must not appear"}

        return wait_for_mock_run(read, "/owned-run", "owned", timeout_seconds=timeout,
                                 clock=lambda: elapsed[0],
                                 sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def test_legitimate_queue_delay_beyond_five_seconds(self):
        self.assertEqual(self.wait(["QUEUED"] * 7 + ["RUNNING", "COMPLETED"])["state"], "COMPLETED")

    def test_terminal_failure_is_immediate_and_sanitized(self):
        for state in ("FAILED", "CANCELLED", "BLOCKED", "DEAD_LETTERED", "unexpected private data"):
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "mock run did not complete") as error:
                self.wait([state])
            self.assertNotIn("private", str(error.exception))

    def test_pending_work_has_a_bounded_deadline(self):
        with self.assertRaisesRegex(TimeoutError, "QUEUED"):
            self.wait(["QUEUED"], timeout=3)

    def test_another_operation_cannot_satisfy_acceptance(self):
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            wait_for_mock_run(lambda _: {"operation_id": "other", "state": "COMPLETED"},
                              "/owned-run", "owned")
