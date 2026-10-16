import unittest

from unittest.mock import patch

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from workers.operations import OperationCancelled


class GreenhouseApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = GreenhouseApplicationAdapter()

    def test_normalizes_supported_https_greenhouse_route(self):
        normalized = self.adapter.normalize_route({
            "provider": "greenhouse",
            "url": "https://job-boards.greenhouse.io/example/jobs/1234?utm_source=fixture",
        })

        self.assertEqual(normalized, {
            "provider": "greenhouse",
            "tenant": "example",
            "requisition": "1234",
            "url": "https://boards.greenhouse.io/example/jobs/1234",
        })

    def test_rejects_untrusted_route_shapes_and_provider_mismatch(self):
        invalid_routes = (
            {"provider": "lever", "url": "https://boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "http://boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io.attacker.test/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io/example/jobs/1234/extra"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io/example/jobs/not-a-requisition"},
            {"provider": "greenhouse", "url": "https://user@boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io:8443/example/jobs/1234"},
        )

        for route in invalid_routes:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_uncertain_fields_require_human_review_and_submission_is_never_supported(self):
        analysis = {
            "status": "PASS",
            "fields": [
                {"sensitivity": "STANDARD", "ambiguous": False},
                {"sensitivity": "SENSITIVE", "ambiguous": False},
            ],
        }

        self.assertTrue(self.adapter.requires_human_review(analysis))
        self.assertFalse(self.adapter.capabilities()["submission"])
        self.assertFalse(self.adapter.capabilities()["live_form_interaction"])

    def test_standard_unambiguous_analysis_does_not_require_manual_review(self):
        analysis = {
            "status": "PASS",
            "fields": [{"sensitivity": "STANDARD", "ambiguous": False}],
        }

        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_unknown_or_malformed_analysis_requires_human_review(self):
        self.assertTrue(self.adapter.requires_human_review({"status": "UNKNOWN", "fields": []}))
        self.assertTrue(self.adapter.requires_human_review(None))

    def test_validate_page_observes_cancellation_immediately(self):
        def check():
            raise OperationCancelled("synthetic cancellation")
        with self.assertRaises(OperationCancelled):
            self.adapter.validate_page(None, check=check)

    def test_validate_page_bounds_eval_timeout_to_remaining_budget(self):
        class MockLocator:
            def __init__(self):
                self.calls = []
            def evaluate_all(self, script, **kwargs):
                self.calls.append(kwargs)
                return [{"validates": True, "valid": True}]

        class MockPage:
            def __init__(self, locator):
                self._locator = locator
            def locator(self, selector):
                return self._locator

        mock_locator = MockLocator()
        mock_page = MockPage(mock_locator)

        class BudgetCheck:
            def __call__(self):
                pass
            def remaining_seconds(self):
                return 3.5

        analysis = {
            "status": "PASS",
            "fields": [{"input_type": "text", "sensitivity": "STANDARD", "ambiguous": False}],
        }
        with patch.object(self.adapter, "analyze_page", return_value=analysis):
            result = self.adapter.validate_page(mock_page, analysis, check=BudgetCheck())
        self.assertEqual(result, {"status": "PASS", "reason": None})
        self.assertEqual(len(mock_locator.calls), 1)
        self.assertEqual(mock_locator.calls[0], {"timeout": 3500})

    def test_validate_page_re_raises_cancellation_on_eval_failure(self):
        cancelled = [False]
        def check():
            if cancelled[0]:
                raise OperationCancelled("cancelled during locator eval")

        class FailingLocator:
            def evaluate_all(self, script, **kwargs):
                cancelled[0] = True
                raise RuntimeError("Evaluation interrupted")

        class MockPage:
            def locator(self, selector):
                return FailingLocator()

        analysis = {
            "status": "PASS",
            "fields": [{"input_type": "text", "sensitivity": "STANDARD", "ambiguous": False}],
        }
        with patch.object(self.adapter, "analyze_page", return_value=analysis):
            with self.assertRaises(OperationCancelled):
                self.adapter.validate_page(MockPage(), analysis, check=check)

    def test_upload_mock_fixture_documents_observes_cancellation(self):
        def check():
            raise OperationCancelled("synthetic cancellation before upload")
        with self.assertRaises(OperationCancelled):
            self.adapter.upload_mock_fixture_documents(None, None, None, check=check)

    def test_analyze_page_observes_cancellation_immediately(self):
        def check():
            raise OperationCancelled("synthetic cancellation before DOM analysis")
        with self.assertRaises(OperationCancelled):
            self.adapter.analyze_page(None, check=check)

    def test_analyze_page_bounds_eval_timeout_to_remaining_budget(self):
        class MockForm:
            def __init__(self):
                self.calls = []

            def evaluate(self, script, **kwargs):
                self.calls.append(kwargs)
                return [{"tag": "input", "name": "email", "input_type": "email", "labels": ["Email"], "options": []}]

        class MockFormsLocator:
            def __init__(self, form):
                self.first = form

            def count(self):
                return 1

        mock_form = MockForm()

        class MockPage:
            def locator(self, selector):
                return MockFormsLocator(mock_form)

        class BudgetCheck:
            def __call__(self):
                pass

            def remaining_seconds(self):
                return 4.2

        result = self.adapter.analyze_page(MockPage(), check=BudgetCheck())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(mock_form.calls), 1)
        self.assertEqual(mock_form.calls[0], {"timeout": 4200})

    def test_analyze_page_re_raises_cancellation_on_dom_eval_failure(self):
        cancelled = [False]

        def check():
            if cancelled[0]:
                raise OperationCancelled("cancelled during DOM eval")

        class FailingForm:
            def evaluate(self, script, **kwargs):
                cancelled[0] = True
                raise RuntimeError("Evaluation error")

        class MockFormsLocator:
            def __init__(self):
                self.first = FailingForm()

            def count(self):
                return 1

        class MockPage:
            def locator(self, selector):
                return MockFormsLocator()

        with self.assertRaises(OperationCancelled):
            self.adapter.analyze_page(MockPage(), check=check)


if __name__ == "__main__":
    unittest.main()
