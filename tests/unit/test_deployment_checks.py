"""Deterministic deployment gate regression tests; no network access."""
from contextlib import redirect_stderr, redirect_stdout
from email.utils import formatdate
import errno
from http.client import BadStatusLine, IncompleteRead
import io
import json
import socket
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools.deploy import wait_for_checks as gate
from tools.deploy.wait_for_checks import MAIN_GATES, assess_checks


SHA = "a" * 40
PRIVATE = "synthetic-token-and-response-canary"


def check(name, conclusion="success", *, id=1, status="completed", app="github-actions"):
    return {"name": name, "conclusion": conclusion, "status": status,
            "started_at": "2026-10-24T00:00:00Z", "id": id, "app": {"slug": app},
            "head_sha": SHA}


class DeploymentCheckTests(unittest.TestCase):
    def test_exact_success_required_for_all_independent_gates(self):
        rows = [check(name, id=i) for i, name in enumerate(MAIN_GATES)]
        self.assertEqual(assess_checks(rows), {name: "SUCCESS" for name in MAIN_GATES})

    def test_missing_pending_and_skipped_are_not_success(self):
        self.assertEqual(assess_checks([]), {name: "MISSING" for name in MAIN_GATES})
        rows = [check(MAIN_GATES[0], status="in_progress"),
                check(MAIN_GATES[1], "skipped")]
        state = assess_checks(rows)
        self.assertEqual(state[MAIN_GATES[0]], "PENDING")
        self.assertEqual(state[MAIN_GATES[1]], "SKIPPED")
        self.assertEqual(state[MAIN_GATES[2]], "MISSING")

    def test_newer_failure_cannot_be_hidden_by_old_success(self):
        rows = [check(MAIN_GATES[0], id=10),
                check(MAIN_GATES[0], conclusion="failure", id=20)]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "FAILURE")

    def test_newer_queued_run_cannot_be_hidden_by_older_completed_run(self):
        queued = check(MAIN_GATES[0], conclusion=None, id=20, status="queued")
        queued["started_at"] = None
        for older_conclusion in ("success", "failure"):
            with self.subTest(older_conclusion=older_conclusion):
                rows = [check(MAIN_GATES[0], conclusion=older_conclusion, id=10), queued]
                self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "PENDING")

    def test_conflicting_duplicate_run_states_are_refused_in_either_order(self):
        success = check(MAIN_GATES[0], id=10)
        failure = check(MAIN_GATES[0], "failure", id=10)
        for rows in ([success, failure], [failure, success]):
            with self.subTest(first=rows[0]["conclusion"]), self.assertRaises(ValueError):
                assess_checks(rows)

    def test_identical_duplicate_runs_remain_unambiguous(self):
        rows = [check(name, id=index) for index, name in enumerate(MAIN_GATES)]
        self.assertEqual(assess_checks(rows + rows), {name: "SUCCESS" for name in MAIN_GATES})

    def test_untrusted_app_cannot_masquerade_as_required_check(self):
        rows = [check(MAIN_GATES[0], "failure", id=100, app="unknown")]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "MISSING")
        rows.append(check(MAIN_GATES[0], id=10))
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "SUCCESS")

    def test_other_successful_workflow_is_not_a_substitute(self):
        rows = [check("Web / Vitest and production build")]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "MISSING")


class DeploymentLookupTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0.0
        self.sleeps = []
        # Keep virtual time local to the gate: unrelated background sleeps
        # must not consume this test's deterministic deadline.
        self.enterContext(patch.object(gate, "time", SimpleNamespace(
            monotonic=lambda: self.elapsed,
            time=lambda: 1700000000 + self.elapsed,
            sleep=self.sleep,
        )))
        self.enterContext(patch.dict(gate.os.environ, {"GH_TOKEN": PRIVATE}))

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.sleeps.append(seconds)
        self.elapsed += seconds

    @staticmethod
    def response(rows=None, *, payload=None):
        if payload is None:
            payload = {"check_runs": rows if rows is not None else [
                check(name, id=index) for index, name in enumerate(MAIN_GATES)]}
        return io.BytesIO(json.dumps(payload).encode())

    @staticmethod
    def http_error(status, headers=None):
        return HTTPError("https://api.github.com/" + PRIVATE, status, PRIVATE,
                         headers or {}, io.BytesIO(PRIVATE.encode()))

    def run_gate(self, replies, *, timeout=10):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(gate, "urlopen", side_effect=replies) as request, \
                redirect_stdout(output), redirect_stderr(error):
            result = gate.main(["--sha", SHA, "--timeout-seconds", str(timeout), "--poll-seconds", "5"])
        self.assertNotIn(PRIVATE, output.getvalue() + error.getvalue())
        for call in request.call_args_list:
            self.assertIn("/commits/" + SHA + "/check-runs?", call.args[0].full_url)
            self.assertEqual(call.args[0].get_method(), "GET")
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 15)
        return result, request, output.getvalue(), error.getvalue()

    def test_transient_http_failure_recovers_with_safe_status_and_closes_error_response(self):
        error = self.http_error(503)
        body = error.fp
        result, request, _, diagnostic = self.run_gate([error, self.response()])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.sleeps, [1])
        self.assertIn("HTTPError status=503", diagnostic)
        self.assertTrue(body.closed)

    def test_other_transient_http_statuses_are_retryable(self):
        for status in (408, 429, 500, 502, 504):
            with self.subTest(status=status):
                result, request, _, _ = self.run_gate([
                    self.http_error(status, {"Retry-After": "1"}), self.response()])
                self.assertEqual((result, request.call_count), (0, 2))

    def test_transient_transport_failures_recover(self):
        for error in (URLError(TimeoutError(PRIVATE)), ConnectionResetError(errno.ECONNRESET, PRIVATE),
                      URLError(socket.gaierror(socket.EAI_AGAIN, PRIVATE)), IncompleteRead(b"partial")):
            with self.subTest(kind=type(error).__name__):
                result, request, _, diagnostic = self.run_gate([error, self.response()])
                self.assertEqual((result, request.call_count), (0, 2))
                self.assertIn(type(error).__name__, diagnostic)

    def test_permanent_http_failures_stop_without_retry(self):
        for status in (400, 401, 403, 404, 422, 501):
            with self.subTest(status=status):
                result, request, _, diagnostic = self.run_gate([self.http_error(status)])
                self.assertEqual((result, request.call_count), (2, 1))
                self.assertIn(f"HTTPError status={status}", diagnostic)
        self.assertEqual(self.sleeps, [])

    def test_permanent_transport_failures_stop_without_retry(self):
        for error in (URLError(ssl.SSLCertVerificationError(1, PRIVATE)),
                      URLError(socket.gaierror(socket.EAI_NONAME, PRIVATE)),
                      URLError(PRIVATE), PermissionError(PRIVATE), BadStatusLine(PRIVATE)):
            with self.subTest(kind=type(error).__name__):
                result, request, _, _ = self.run_gate([error])
                self.assertEqual((result, request.call_count), (2, 1))
        self.assertEqual(self.sleeps, [])

    def test_error_response_cleanup_failure_stops_with_safe_diagnostics(self):
        error = self.http_error(503)
        with patch.object(error, "close", side_effect=OSError(PRIVATE)):
            result, request, _, diagnostic = self.run_gate([error])
        error.close()
        self.assertEqual((result, request.call_count), (2, 1))
        self.assertIn("HTTPError status=503", diagnostic)
        self.assertEqual(self.sleeps, [])

    def test_transient_retries_cannot_reset_the_original_deadline(self):
        result, request, _, _ = self.run_gate(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(self.http_error(502)), timeout=2)
        self.assertEqual(result, 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.sleeps, [1, 1])
        self.assertEqual(self.elapsed, 2)
        self.assertEqual([call.kwargs["timeout"] for call in request.call_args_list], [2, 1])

    def test_consecutive_transient_retries_have_a_limit_before_deadline(self):
        result, request, _, _ = self.run_gate(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(self.http_error(503)), timeout=120)
        self.assertEqual(result, 2)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(self.sleeps, [1, 2, 4])
        self.assertLess(self.elapsed, 120)

    def test_retry_after_is_honored_and_clipped_to_remaining_deadline(self):
        result, request, _, _ = self.run_gate([self.http_error(429, {"Retry-After": "60"})], timeout=2)
        self.assertEqual((result, request.call_count), (1, 1))
        self.assertEqual(self.sleeps, [2])

    def test_rate_limit_without_retry_after_does_not_retry_early(self):
        result, request, _, _ = self.run_gate([self.http_error(429)], timeout=2)
        self.assertEqual((result, request.call_count), (1, 1))
        self.assertEqual(self.sleeps, [2])

    def test_retry_after_http_date_is_honored(self):
        result, request, _, _ = self.run_gate([
            self.http_error(503, {"Retry-After": formatdate(1700000004, usegmt=True)}), self.response()])
        self.assertEqual((result, request.call_count), (0, 2))
        self.assertEqual(self.sleeps, [4])

    def test_invalid_retry_after_is_not_a_reason_to_retry_early(self):
        for value in (PRIVATE, "-1", "nan", "inf"):
            with self.subTest(value=value):
                result, request, _, _ = self.run_gate([self.http_error(429, {"Retry-After": value})])
                self.assertEqual((result, request.call_count), (2, 1))
        self.assertEqual(self.sleeps, [])

    def test_malformed_response_is_refused_without_retry(self):
        malformed = [[], {}, {"check_runs": PRIVATE}, {"check_runs": [None]},
                     {"check_runs": [{**check(MAIN_GATES[0]), "head_sha": "b" * 40}]},
                     {"check_runs": [{**check(MAIN_GATES[0]), "app": None}]},
                     {"check_runs": [{**check(MAIN_GATES[0]), "id": PRIVATE}]},
                     {"check_runs": [{**check(MAIN_GATES[0]), "status": PRIVATE}]},
                     {"check_runs": [{**check(MAIN_GATES[0]), "conclusion": PRIVATE}]}]
        for payload in malformed:
            with self.subTest(payload_type=type(payload).__name__):
                result, request, _, _ = self.run_gate([self.response(payload=payload)])
                self.assertEqual((result, request.call_count), (2, 1))
        result, request, _, _ = self.run_gate([io.BytesIO((PRIVATE + " not JSON").encode())])
        self.assertEqual((result, request.call_count), (2, 1))
        self.assertEqual(self.sleeps, [])

    def test_failed_required_check_after_recovery_is_not_retried(self):
        rows = [check(name, "failure" if index == 0 else "success", id=index)
                for index, name in enumerate(MAIN_GATES)]
        result, request, output, _ = self.run_gate([self.http_error(503), self.response(rows)])
        self.assertEqual((result, request.call_count), (1, 2))
        self.assertIn('"FAILURE"', output)
        self.assertEqual(self.sleeps, [1])

    def test_newest_queued_rerun_blocks_deployment_despite_older_success(self):
        rows = [check(name, id=index) for index, name in enumerate(MAIN_GATES)]
        rows.append({**check(MAIN_GATES[0], conclusion=None, status="queued", id=100),
                     "started_at": None})
        result, request, output, _ = self.run_gate([self.response(rows)], timeout=2)
        self.assertEqual((result, request.call_count), (1, 1))
        self.assertIn('"PENDING"', output)
        self.assertEqual(self.sleeps, [2])

    def test_conflicting_duplicate_failure_cannot_publish_success(self):
        rows = [check(name, id=index) for index, name in enumerate(MAIN_GATES)]
        rows.append(check(MAIN_GATES[0], "failure", id=0))
        result, request, output, _ = self.run_gate([self.response(rows)])
        self.assertEqual((result, request.call_count), (2, 1))
        self.assertNotIn('"SUCCESS"', output)
        self.assertEqual(self.sleeps, [])

    def test_pagination_failure_discards_partial_success_before_retry(self):
        first_page = [check(name, id=index) for index, name in enumerate(MAIN_GATES)]
        first_page.extend(check("unrelated", id=index) for index in range(3, 100))
        result, request, _, _ = self.run_gate([
            self.response(first_page), self.http_error(502), self.response(first_page),
            self.response([check(MAIN_GATES[0], "failure", id=1000)])])
        self.assertEqual((result, request.call_count), (1, 4))
        self.assertEqual([call.args[0].full_url.rsplit("page=", 1)[1] for call in request.call_args_list],
                         ["1", "2", "1", "2"])

    def test_full_pagination_limit_remains_fail_closed(self):
        rows = [check("unrelated", id=index) for index in range(100)]
        result, request, _, _ = self.run_gate([self.response(rows) for _ in range(5)])
        self.assertEqual((result, request.call_count), (2, 5))
        self.assertEqual(self.sleeps, [])

    def test_each_page_request_timeout_uses_remaining_budget(self):
        rows = [check(name, id=index) for index, name in enumerate(MAIN_GATES)]
        rows.extend(check("unrelated", id=index) for index in range(3, 100))
        replies = iter([self.response(rows), self.response([])])

        def respond(*_args, **_kwargs):
            response = next(replies)
            self.elapsed = 8
            return response

        result, request, _, _ = self.run_gate(respond)
        self.assertEqual(result, 0)
        self.assertEqual([call.kwargs["timeout"] for call in request.call_args_list], [10, 2])

    def test_success_received_after_deadline_cannot_pass(self):
        def late(*_args, **_kwargs):
            self.elapsed = 11
            return self.response()

        result, request, output, _ = self.run_gate(late)
        self.assertEqual((result, request.call_count), (1, 1))
        self.assertNotIn('"SUCCESS"', output)

    def test_pending_poll_sleep_is_clipped_to_deadline(self):
        result, request, _, _ = self.run_gate([self.response([])], timeout=3)
        self.assertEqual((result, request.call_count), (1, 1))
        self.assertEqual(self.sleeps, [3])

    def test_zero_budget_never_starts_a_request(self):
        result, request, _, _ = self.run_gate([], timeout=0)
        self.assertEqual((result, request.call_count), (1, 0))
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
