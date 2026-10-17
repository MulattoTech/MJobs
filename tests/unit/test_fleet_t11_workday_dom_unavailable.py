"""Deferred Workday DOM entry points must never touch a caller-owned Page."""
from concurrent.futures import CancelledError
import unittest

from services.applications.adapters.workday import WorkdayApplicationAdapter


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
