"""Deferred Workday DOM entry points must never touch a caller-owned Page."""
from concurrent.futures import CancelledError
import unittest

from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.provider_registry import capabilities, get_provider


class WorkdayDOMUnavailableTests(unittest.TestCase):
    def test_page_entry_points_fail_closed_without_any_page_access(self):
        accesses = []

        class Page:
            def __getattribute__(self, name):
                accesses.append(name)
                raise AssertionError("Deferred inspection must not access Page")

        adapter = WorkdayApplicationAdapter()
        for method in (adapter.analyze_page, adapter.validate_page):
            with self.subTest(method=method.__name__):
                checks = []
                report = method(Page(), check=lambda: checks.append(1))
                self.assertEqual(report["status"], "UNKNOWN")
                self.assertEqual(report["reason"], "WORKDAY_DOM_INSPECTION_UNAVAILABLE")
                self.assertNotIn("dynamic_review", report)
                self.assertEqual(checks, [1])
                self.assertEqual(accesses, [])

    def test_page_entry_points_preserve_cancellation_identity_without_page_access(self):
        cancellation = CancelledError("Synthetic cancellation")

        def cancelled():
            raise cancellation

        class Page:
            def __getattribute__(self, name):
                raise AssertionError("Cancellation must precede all Page access")

        adapter = WorkdayApplicationAdapter()
        for method in (adapter.analyze_page, adapter.validate_page):
            with self.subTest(method=method.__name__):
                with self.assertRaises(CancelledError) as caught:
                    method(Page(), check=cancelled)
                self.assertIs(caught.exception, cancellation)


    def test_disabled_fixture_upload_does_not_inspect_browser_or_private_documents(self):
        accesses = []

        class PrivateInput:
            def __getattribute__(self, name):
                accesses.append(name)
                raise AssertionError("Unavailable upload must not inspect inputs")

        adapter = get_provider("workday")
        self.assertIs(capabilities("workday")["mock_fixture_upload"], False)
        checks = []
        with self.assertRaisesRegex(ValueError, "^WORKDAY_FIXTURE_UPLOAD_UNAVAILABLE$"):
            adapter.upload_mock_fixture_documents(
                PrivateInput(), PrivateInput(), PrivateInput(), check=lambda: checks.append(1),
            )
        self.assertEqual(accesses, [])
        self.assertEqual(checks, [1])

        with self.assertRaisesRegex(ValueError, "^WORKDAY_FIXTURE_UPLOAD_UNAVAILABLE$"):
            adapter.upload_mock_fixture_documents(PrivateInput(), None, None)
        self.assertEqual(accesses, [])

    def test_disabled_fixture_upload_preserves_cancellation_and_deadline_identity(self):
        class PrivateInput:
            def __getattribute__(self, name):
                raise AssertionError("Cancellation must precede all input access")

        for failure in (CancelledError("Synthetic cancellation"), TimeoutError("Synthetic deadline")):
            with self.subTest(failure=type(failure).__name__):
                calls = []

                def stopped():
                    calls.append(1)
                    raise failure

                with self.assertRaises(type(failure)) as caught:
                    get_provider("workday").upload_mock_fixture_documents(
                        PrivateInput(), PrivateInput(), PrivateInput(), check=stopped,
                    )
                self.assertIs(caught.exception, failure)
                self.assertEqual(calls, [1])
