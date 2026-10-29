"""Application log context is a value-free persisted projection."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as flow
from tests.integration.test_application_operation_logs import ApplicationLogFixture
from workers.dispatch import enqueue_operation


PRIVATE_NAME = 'Riley "RJ" Chen — token=SYNTHETIC_PRIVATE_FIELD'
PRIVATE_EMAIL = 'riley.private+application@example.invalid'
PRIVATE_VALUES = (PRIVATE_NAME, PRIVATE_EMAIL, 'SYNTHETIC_PRIVATE_FIELD')


class ControlledApplicationFieldPrivacyTests(ApplicationLogFixture, unittest.TestCase):
    def setUp(self):
        from tests.integration import test_spine

        profile = copy.deepcopy(test_spine.PROFILE)
        profile.update(name=PRIVATE_NAME, email=PRIVATE_EMAIL)
        with patch.object(test_spine, 'PROFILE', profile):
            super().setUp()

    def private_controlled_run(self, request_id):
        profile = self.store.profile('fixture')
        self.assertEqual((profile['name'], profile['email']), (PRIVATE_NAME, PRIVATE_EMAIL))
        preview, body, queued = self.review_and_queue(request_id)
        self.assertEqual(
            {field['value'] for field in preview['fields'] if field['source'] == 'CONTACT'},
            {PRIVATE_NAME, PRIVATE_EMAIL},
        )
        return preview, body, queued

    def assert_private_values_absent(self, value):
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        for private in PRIVATE_VALUES:
            self.assertNotIn(private, serialized)

    def related_audit_and_operation(self, operation_id):
        with self.store.engine.connect() as connection:
            audits = [dict(row) for row in connection.execute(select(
                m.audit.c.action, m.audit.c.payload,
            ).where(m.audit.c.entity_id.in_([
                self.application_id, operation_id,
            ]))).mappings()]
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())
        return audits, operation

    def test_controlled_field_values_stay_out_of_public_receipt_audit_and_log(self):
        _, body, queued = self.private_controlled_run('private-field-success')
        observed = []

        def synthetic_browser(current, _prepared, _supporting, *, check):
            check()
            observed.extend(field['value'] for field in current['fields']
                            if field['source'] == 'CONTACT')
            return {
                'documents': [],
                'pages': [
                    {'id': 'contact', 'control_count': 3, 'validation': 'PASS'},
                    {'id': 'documents', 'control_count': 3, 'validation': 'PASS'},
                ],
            }

        with self.assertLogs('mjobs.operations', level='INFO') as captured, patch(
                'services.applications.controlled_dry_run.run_browser', synthetic_browser):
            result = self.execute(queued['operation_id'])

        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertCountEqual(observed, [PRIVATE_NAME, PRIVATE_EMAIL])
        public = self.client.get(self.path() + '/runs/' + body['request_id'])
        self.assertEqual(public.status_code, 200, public.text)
        self.assertEqual(public.json()['state'], 'COMPLETED')
        self.assertIsNotNone(public.json()['receipt'])
        audits, operation = self.related_audit_and_operation(queued['operation_id'])
        self.assertTrue(any(row['action'] == 'APPLICATION_DRY_RUN' for row in audits))
        self.assert_private_values_absent(public.json())
        self.assert_private_values_absent(audits)
        self.assert_private_values_absent(operation['params'])
        self.assert_private_values_absent([record.getMessage() for record in captured.records])

    def test_controlled_field_values_and_browser_diagnostics_stay_out_of_errors_audit_and_log(self):
        _, body, queued = self.private_controlled_run('private-field-failure')
        diagnostic = RuntimeError(
            f'browser rejected applicant {PRIVATE_NAME} at {PRIVATE_EMAIL}'
        )

        with self.assertLogs('mjobs.operations', level='INFO') as captured, patch(
                'services.applications.controlled_dry_run.run_browser', side_effect=diagnostic):
            result = self.execute(queued['operation_id'])

        self.assertEqual(result, {
            'operation_id': queued['operation_id'],
            'status': 'BLOCKED',
            'error_category': 'CONTROLLED_DRY_RUN_BROWSER_FAILED',
        })
        public = self.client.get(self.path() + '/runs/' + body['request_id'])
        self.assertEqual(public.status_code, 200, public.text)
        self.assertEqual(public.json()['error_code'], 'CONTROLLED_DRY_RUN_BROWSER_FAILED')
        self.assertIsNone(public.json()['receipt'])
        audits, operation = self.related_audit_and_operation(queued['operation_id'])
        self.assert_private_values_absent(public.json())
        self.assert_private_values_absent(audits)
        self.assert_private_values_absent(operation['params'])
        self.assert_private_values_absent(operation['error'])
        self.assert_private_values_absent([record.getMessage() for record in captured.records])


class ApplicationLogPrivacyTests(ApplicationLogFixture, unittest.TestCase):
    def test_missing_malformed_and_nonapplication_entities_never_become_log_context(self):
        for kind, expected_state in (("APPLICATION_FORM_REVIEW", "DEAD_LETTERED"),
                                     ("APPLICATION_DRY_RUN", "BLOCKED")):
            for entity_id in ("missing-application", "fixture",
                              "https://private.test/?token=SYNTHETIC_PRIVATE"):
                with self.subTest(kind=kind, entity_id=entity_id):
                    operation_id = enqueue_operation(self.store, kind, entity_id, {
                        "application_id": self.application_id, "job_id": "forged-job",
                        "company_id": "forged-company", "ats_provider": "greenhouse",
                        "api_key": "SYNTHETIC_PRIVATE",
                    })
                    with self.assertLogs("mjobs.operations", level="INFO") as captured:
                        result = self.execute(operation_id)
                    self.assertEqual(result["status"], expected_state, result)
                    message = captured.records[0].getMessage()
                    record = json.loads(message)
                    self.assertEqual(record["result"], expected_state)
                    for field in ("application_id", "job_id", "company_id", "ats", "params"):
                        self.assertNotIn(field, record)
                    for value in ("SYNTHETIC_PRIVATE", "forged-", "private.test", "fixture"):
                        self.assertNotIn(value, message)
                    with self.store.engine.connect() as connection:
                        state = connection.execute(select(m.operations.c.state).where(
                            m.operations.c.id == operation_id)).scalar_one()
                    self.assertEqual(state, expected_state)

    def test_form_review_ignores_private_payload_and_forged_context(self):
        path, operation_id = self.queue_review("private-review")
        with self.store.engine.begin() as connection:
            params = connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == operation_id)).scalar_one()
            params.update(job_id="forged-job", company_id="forged-company", ats_provider="lever",
                          api_key="SYNTHETIC_PRIVATE_KEY", answer="SYNTHETIC_PRIVATE_ANSWER",
                          resume_text="SYNTHETIC_PRIVATE_RESUME")
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(params=params))
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            result = self.execute_review(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        message = captured.records[0].getMessage()
        self.assert_context(json.loads(message), "APPLICATION_FORM_REVIEW", "COMPLETED")
        for value in ("SYNTHETIC_PRIVATE", "forged-", "lever", "example/jobs", "alex@example.test",
                      "profile_id", "params", "route_url", "tenant", "request_id", "analysis"):
            self.assertNotIn(value, message)
        self.assertEqual(self.client.get(path + "/private-review").json()["state"], "COMPLETED")

    def test_lookup_failure_preserves_terminal_outcomes_without_diagnostic_values(self):
        path, review = self.queue_review("lookup-failed")
        _, body, dry_run = self.review_and_queue("lookup-failed-dry-run")
        for kind, operation_id, execute in (
            ("APPLICATION_FORM_REVIEW", review, self.execute_review),
            ("APPLICATION_DRY_RUN", dry_run["operation_id"], self.execute),
        ):
            with self.subTest(kind=kind), self.assertLogs("mjobs.operations", level="INFO") as captured, patch(
                    "workers.tasks._resolve_application_log_context",
                    side_effect=RuntimeError("SYNTHETIC_PRIVATE_DATABASE_DIAGNOSTIC")):
                result = execute(operation_id)
            self.assertEqual(result["status"], "COMPLETED", result)
            message = captured.records[0].getMessage()
            record = json.loads(message)
            self.assertEqual(record["result"], "COMPLETED")
            for field in ("application_id", "job_id", "company_id", "ats", "params"):
                self.assertNotIn(field, record)
            self.assertNotIn("SYNTHETIC_PRIVATE", message)
        self.reopen()
        self.assertEqual(self.client.get(path + "/lookup-failed").json()["state"], "COMPLETED")
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertIsNotNone(saved["receipt"])


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Requires isolated disposable PostgreSQL")
class ApplicationLogPrivacyPostgresTests(ApplicationLogPrivacyTests):
    _new_store = flow.ControlledApplicationDryRunPostgresTests._new_store
    _drop_schema = flow.ControlledApplicationDryRunPostgresTests._drop_schema
