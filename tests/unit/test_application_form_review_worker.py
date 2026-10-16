"""Durable, read-only Greenhouse form review worker behavior."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled
from workers.application_form_review import FormReviewError, handle_form_review


SAFE_ANALYSIS = {
    "status": "REVIEW_REQUIRED",
    "reason": "FORM_REQUIRES_HUMAN_REVIEW",
    "fields": [{
        "id": "email", "name": "email", "type": "email",
        "required": True, "review_reason": None,
    }],
}


class ApplicationFormReviewWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'form-review.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture")
                       if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.application = self.store.application(self.application_id)
        self.route_url = "https://boards.greenhouse.io/example/jobs/12345"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.application["route_id"],
            ).values(provider="greenhouse", tenant="example", requisition="12345",
                     url=self.route_url))
        self.application = self.store.application(self.application_id)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def make_operation(self, **overrides):
        params = {
            "profile_id": "fixture",
            "application_id": self.application_id,
            "route_id": self.application["route_id"],
            "route_url": self.route_url,
            "provider": "greenhouse",
            "request_id": "review-request-001",
            "input_fingerprint": "a" * 64,
        }
        params.update(overrides)
        operation_id = identity("APPLICATION_FORM_REVIEW", self.application_id, params)
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="APPLICATION_FORM_REVIEW",
                entity_id=self.application_id, params=params, state="RUNNING",
                error=None, updated_at=now(), attempt_count=1,
                cancel_requested=False,
                lease_expires_at=(datetime.now(timezone.utc)
                                  + timedelta(minutes=2)).isoformat(),
                last_attempt_at=now(),
            ))
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())

    def with_analysis_service(self, report=SAFE_ANALYSIS):
        parser = Mock(return_value=copy.deepcopy(report))
        # Patch the import actually used by the worker; the legacy parser
        # module was replaced by the provider-aware analysis interface.
        return patch("workers.application_form_review.analyze_application_form", parser), parser

    def audit_events(self, operation_id):
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings()
            return [dict(row) for row in rows
                    if row["payload"].get("operation_id") == operation_id]

    def configure_provider_route(self, provider, url, tenant, requisition):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.application["route_id"],
            ).values(provider=provider, tenant=tenant, requisition=requisition, url=url))
        self.application = self.store.application(self.application_id)

    def test_workday_durable_dispatch_is_synthetic_value_free_and_manual_review_only(self):
        route_url = (
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/"
            "San-Francisco/Software_Engineer_R12345"
        )
        self.configure_provider_route("workday", route_url, "acme", "R12345")
        forms = (
            ('<form><label for="email">Email address</label>'
             '<input id="email" name="email" type="email" required '
             'value="PRIVATE_VALUE"></form>', "MANUAL_REVIEW_REQUIRED", None),
            ('<form><label for="question">What do you enjoy learning?</label>'
             '<textarea id="question" name="question">PRIVATE_ANSWER</textarea></form>',
             "FORM_REQUIRES_HUMAN_REVIEW", "UNKNOWN_QUESTION"),
            ('<form><input id="ssn" name="ssn" value="PRIVATE_SSN"></form>',
             "FORM_REQUIRES_HUMAN_REVIEW", "SENSITIVE_FIELD"),
            ('<form><div class="cf-turnstile">PRIVATE_CHALLENGE</div>'
             '<input id="email" name="email" type="email"></form>',
             "CHALLENGE_PROTECTED", None),
        )

        for index, (html, reason, field_reason) in enumerate(forms):
            with self.subTest(reason=reason, index=index):
                operation = self.make_operation(
                    route_url=route_url, provider="workday",
                    request_id=f"workday-review-{index}",
                    input_fingerprint=str(index + 1) * 64,
                )
                fetch_html = Mock(return_value=html)
                result = handle_form_review(
                    self.store, operation, fetch_html=fetch_html, check=lambda: None,
                )
                self.assertEqual(result["status"], "REVIEW_REQUIRED")
                self.assertEqual(result["analysis"]["reason"], reason)
                if field_reason:
                    self.assertIn(field_reason, [
                        field["review_reason"] for field in result["analysis"]["fields"]
                    ])
                fetch_html.assert_called_once_with(route_url)
                serialized = json.dumps(self.audit_events(operation["id"])[0]["payload"])
                for private in ("PRIVATE_VALUE", "PRIVATE_ANSWER", "PRIVATE_SSN",
                                "PRIVATE_CHALLENGE", "What do you enjoy learning?"):
                    self.assertNotIn(private, serialized)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_greenhouse_and_lever_durable_dispatch_reports_remain_unchanged(self):
        providers = (
            ("greenhouse", "https://boards.greenhouse.io/example/jobs/12345",
             "example", "12345",
             '<form><label for="email">Email address</label>'
             '<input id="email" name="email" type="email" required></form>',
             {"id": "email", "name": "email", "type": "email",
              "required": True, "review_reason": None}),
            ("lever", "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000",
             "example", "123e4567-e89b-12d3-a456-426614174000",
             '<form><label for="name">Full name</label>'
             '<input id="name" name="full_name" type="text"></form>',
             {"id": "name", "name": "full_name", "type": "text",
              "required": False, "review_reason": None}),
        )
        for index, (provider, route_url, tenant, requisition, html, field) in enumerate(providers):
            with self.subTest(provider=provider):
                self.configure_provider_route(provider, route_url, tenant, requisition)
                operation = self.make_operation(
                    provider=provider, route_url=route_url,
                    request_id=f"unchanged-{provider}",
                    input_fingerprint=str(index + 5) * 64,
                )
                result = handle_form_review(
                    self.store, operation, fetch_html=Mock(return_value=html),
                    check=lambda: None,
                )
                self.assertEqual(result["status"], "ANALYZED")
                self.assertIsNone(result["analysis"]["reason"])
                self.assertEqual(result["analysis"]["fields"], [field])
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_workday_tenant_or_requisition_mismatch_fails_before_fetch(self):
        route_url = (
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/"
            "San-Francisco/Software_Engineer_R12345"
        )
        invalid_metadata = (("other", "R12345"), ("acme", "R999"))
        for index, (tenant, requisition) in enumerate(invalid_metadata):
            with self.subTest(tenant=tenant, requisition=requisition):
                self.configure_provider_route("workday", route_url, tenant, requisition)
                operation = self.make_operation(
                    route_url=route_url, provider="workday",
                    request_id=f"mismatch-{index}",
                    input_fingerprint=str(index + 7) * 64,
                )
                fetch_html = Mock(
                    side_effect=AssertionError("mismatched metadata must not fetch")
                )
                with self.assertRaises(FormReviewError) as raised:
                    handle_form_review(
                        self.store, operation, fetch_html=fetch_html, check=lambda: None,
                    )
                self.assertEqual(raised.exception.code, "FORM_REVIEW_ROUTE_UNSUPPORTED")
                fetch_html.assert_not_called()
                self.assertEqual(self.audit_events(operation["id"]), [])

    def test_workday_metadata_case_is_canonicalized_like_adapter(self):
        route_url = (
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/"
            "San-Francisco/Software_Engineer_R12345"
        )
        self.configure_provider_route("workday", route_url, "ACME", "r12345")
        operation = self.make_operation(route_url=route_url, provider="workday")
        fetch_html = Mock(return_value=(
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email"></form>'
        ))

        result = handle_form_review(
            self.store, operation, fetch_html=fetch_html, check=lambda: None,
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["analysis"]["reason"], "MANUAL_REVIEW_REQUIRED")
        fetch_html.assert_called_once_with(route_url)

    def test_review_persists_only_safe_report_and_replay_does_not_fetch_again(self):
        operation = self.make_operation()
        html = "<form>PRIVATE_CURRENT_ANSWER raw page body</form>"
        fetch_html = Mock(return_value=html)
        service_patch, parser = self.with_analysis_service()
        with service_patch:
            result = handle_form_review(
                self.store, operation, fetch_html=fetch_html, check=lambda: None,
            )
            replay = handle_form_review(
                self.store, operation,
                fetch_html=Mock(side_effect=AssertionError("replay refetched")),
                check=lambda: None,
            )

        self.assertEqual(result["operation_id"], operation["id"])
        self.assertEqual(result["analysis"], SAFE_ANALYSIS)
        self.assertEqual(replay, result)
        fetch_html.assert_called_once_with(self.route_url)
        parser.assert_called_once_with(
            "greenhouse", html, route_url=self.route_url,
        )
        events = self.audit_events(operation["id"])
        self.assertEqual(len(events), 1)
        serialized = json.dumps(events[0]["payload"])
        self.assertNotIn("PRIVATE_CURRENT_ANSWER", serialized)
        self.assertNotIn("raw page body", serialized)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_unsupported_or_stale_route_fails_before_any_fetch(self):
        operation = self.make_operation(route_url="https://evil.example/jobs/12345")
        fetch_html = Mock(side_effect=AssertionError("must not fetch unsupported route"))
        with self.assertRaises(FormReviewError) as raised:
            handle_form_review(
                self.store, operation, fetch_html=fetch_html, check=lambda: None,
            )
        self.assertEqual(raised.exception.code, "FORM_REVIEW_INPUT_STALE")
        fetch_html.assert_not_called()
        self.assertEqual(self.audit_events(operation["id"]), [])

    def test_cancellation_after_fetch_prevents_analysis_persistence(self):
        operation = self.make_operation()
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise OperationCancelled("internal-only cancellation detail")

        service_patch, parser = self.with_analysis_service()
        with service_patch:
            with self.assertRaises(OperationCancelled):
                handle_form_review(
                    self.store, operation,
                    fetch_html=lambda _url: "<form>PRIVATE_CURRENT_ANSWER</form>",
                    check=check,
                )
        parser.assert_not_called()
        self.assertEqual(self.audit_events(operation["id"]), [])

    def test_fetch_exception_becomes_fixed_code_without_remote_detail(self):
        operation = self.make_operation()
        service_patch, parser = self.with_analysis_service()
        with service_patch:
            with self.assertRaises(FormReviewError) as raised:
                handle_form_review(
                    self.store, operation,
                    fetch_html=lambda _url: (_ for _ in ()).throw(
                        RuntimeError("private provider response and page contents")
                    ),
                    check=lambda: None,
                )
        self.assertEqual(raised.exception.code, "FORM_REVIEW_FETCH_FAILED")
        self.assertNotIn("private provider response", str(raised.exception))
        parser.assert_not_called()
        self.assertEqual(self.audit_events(operation["id"]), [])


if __name__ == "__main__":
    unittest.main()
