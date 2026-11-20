"""Bounded JSON-only CLI reads of the public operation progress projection."""
import io
import json
import unittest

from cli.operation_progress import (
    DEFAULT_API, MAX_RESPONSE_BYTES, run, validate_api_origin, validate_interval,
    validate_max_wait, validate_timeout,
)


def response(operations, **extra):
    return 200, json.dumps({"operations": operations, **extra}).encode()


def operation(state="RUNNING", **extra):
    return {
        "operation_id": "op-1", "operation_type": "DISCOVER", "entity_id": "profile-1",
        "state": state, "current_step": "FETCHING_POSTINGS", "progress": 25,
        "started_at": "2026-11-14T10:00:00+00:00", "updated_at": "2026-11-14T10:01:00+00:00",
        "completed_at": None, "retry_count": 0, "recovery_summary": "RETRY: OPERATION_TIMEOUT",
        "next_attempt_at": None, "error_category": "NETWORK_ERROR", **extra,
    }


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class OperationProgressCliTests(unittest.TestCase):
    def setUp(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def invoke(self, mode, fetch, *, clock=None, timeout=5, interval=2, max_wait=300, profile="profile-1"):
        timer = clock or Clock()
        return run(mode, profile=profile, api=DEFAULT_API, timeout=timeout, interval=interval,
                   max_wait=max_wait, request=fetch, monotonic=timer.monotonic, sleep=timer.sleep,
                   stdout=self.stdout, stderr=self.stderr)

    def snapshots(self):
        return [json.loads(line) for line in self.stdout.getvalue().splitlines()]

    def test_bounds_and_loopback_origin_validation(self):
        self.assertEqual(validate_timeout("30"), 30)
        self.assertEqual(validate_interval("0.25"), .25)
        self.assertEqual(validate_max_wait("3600"), 3600)
        for value in ("0", "30.01", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_timeout(value)
        with self.assertRaises(ValueError):
            validate_timeout(True)
        for value in ("0.1", "30.1", "nan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_interval(value)
        for value in ("0.9", "3601", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_max_wait(value)
        for value in ("http://localhost:18700", "http://192.0.2.1", "http://127.0.0.1/path",
                      "http://127.0.0.1?", "http://127.0.0.1#",
                      "http://user:secret@127.0.0.1", "http://127.0.0.1/?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_api_origin(value)
        self.assertEqual(validate_api_origin("https://[::1]:18700/"), "https://[::1]:18700")

    def test_list_encodes_profile_and_emits_only_safe_allowlisted_json(self):
        seen = []
        unsafe = operation(params={"api_token": "secret"}, error="PRIVATE", recovery_metadata={"raw": "private"},
                           preparation_priority={"tier": 2, "effective_tier": 1, "queue_priority": "NORMAL",
                               "enqueued_at": "2026-11-14T10:00:00Z", "aging_interval_seconds": 3600,
                               "basis": "APPROVED_SKILL_EVIDENCE_AND_POSTING_AGE", "private": "drop"},
                           unknown_field="drop me")

        def fetch(api, path, deadline):
            seen.append((api, path, deadline))
            return response([unsafe], internal="must not print")

        self.assertEqual(self.invoke("list", fetch, profile="profile/with space"), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "/profiles/profile%2Fwith%20space/operations/progress")
        self.assertEqual(seen[0][2], 5)
        snapshot = self.snapshots()[0]
        expected = {key: unsafe[key] for key in (
            "operation_id", "operation_type", "entity_id", "state", "current_step", "progress",
            "started_at", "updated_at", "completed_at", "retry_count", "recovery_summary",
            "next_attempt_at", "error_category",
        )}
        expected["preparation_priority"] = {key: unsafe["preparation_priority"][key] for key in (
            "tier", "effective_tier", "queue_priority", "enqueued_at", "aging_interval_seconds", "basis",
        )}
        self.assertEqual(snapshot, {"operations": [expected]})
        self.assertEqual(set(snapshot["operations"][0]), {
            "operation_id", "operation_type", "entity_id", "state", "current_step", "progress",
            "started_at", "updated_at", "completed_at", "retry_count", "recovery_summary",
            "next_attempt_at", "error_category", "preparation_priority",
        })
        self.assertEqual(set(snapshot["operations"][0]["preparation_priority"]), {
            "tier", "effective_tier", "queue_priority", "enqueued_at", "aging_interval_seconds", "basis",
        })
        output = self.stdout.getvalue()
        for secret in ("secret", "PRIVATE", "recovery_metadata", "unknown_field", "internal"):
            self.assertNotIn(secret, output)
        self.assertTrue(all(line.startswith("{") for line in output.splitlines()))
        self.assertEqual(self.stderr.getvalue(), "")

    def test_watch_prints_changes_serially_and_exits_on_terminal_snapshot(self):
        clock = Clock()
        states = iter([response([operation()]), response([operation()]),
                       response([operation("COMPLETED", progress=100)])])
        in_flight = 0
        maximum = 0

        def fetch(_api, _path, _deadline):
            nonlocal in_flight, maximum
            in_flight += 1
            maximum = max(maximum, in_flight)
            try:
                return next(states)
            finally:
                in_flight -= 1

        self.assertEqual(self.invoke("watch", fetch, clock=clock), 0)
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual(maximum, 1)
        self.assertEqual(clock.sleeps, [2, 2])
        self.assertEqual(self.snapshots()[-1]["operations"][0]["state"], "COMPLETED")

    def test_empty_or_terminal_list_and_watch_exit_without_waiting(self):
        for ops in ([], [operation("FAILED")], [operation("DISCARDED")]):
            clock = Clock()
            self.stdout = io.StringIO()
            self.assertEqual(self.invoke("watch", lambda *_: response(ops), clock=clock), 0)
            self.assertEqual(clock.sleeps, [])
            self.assertEqual(len(self.snapshots()), 1)

    def test_watch_timeout_preserves_latest_safe_snapshot_and_returns_one(self):
        clock = Clock()
        seen = 0

        def fetch(*_):
            nonlocal seen
            seen += 1
            return response([operation(progress=25 if seen == 1 else 50)])

        self.assertEqual(self.invoke("watch", fetch, clock=clock, interval=.25, max_wait=1), 1)
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual(self.snapshots()[-1]["operations"][0]["progress"], 50)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_request_deadline_uses_injected_clock(self):
        clock = Clock()

        def late_response(*_):
            clock.now = 5.0
            return response([operation()])

        self.assertEqual(self.invoke("list", late_response, clock=clock, timeout=5), 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), '{"error":"operation progress unavailable"}\n')

    def test_initial_watch_timeout_without_snapshot_is_generic_read_failure(self):
        clock = Clock()

        def late_response(*_):
            clock.now = 1.1
            return response([operation()])

        self.assertEqual(self.invoke("watch", late_response, clock=clock, max_wait=1), 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), '{"error":"operation progress unavailable"}\n')

    def test_network_http_oversize_and_malformed_errors_are_generic(self):
        failures = [
            lambda *_: (_ for _ in ()).throw(OSError("secret connection string")),
            lambda *_: (503, b"upstream credentials"),
            lambda *_: (200, b"x" * (MAX_RESPONSE_BYTES + 1)),
            lambda *_: (200, b"not-json secret"),
            lambda *_: (200, b'{"operations":{}}'),
            lambda *_: (200, b'{"operations":[null]}'),
        ]
        for fetch in failures:
            with self.subTest(fetch=fetch):
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                self.assertEqual(self.invoke("list", fetch), 2)
                self.assertEqual(self.stdout.getvalue(), "")
                self.assertEqual(self.stderr.getvalue(), '{"error":"operation progress unavailable"}\n')

    def test_invalid_arguments_fail_before_request(self):
        calls = []
        fetch = lambda *args: calls.append(args)
        self.assertEqual(run("watch", profile="", request=fetch, stdout=self.stdout, stderr=self.stderr), 2)
        self.assertEqual(calls, [])
        self.assertEqual(self.stderr.getvalue(), '{"error":"operation progress unavailable"}\n')


if __name__ == "__main__":
    unittest.main()
