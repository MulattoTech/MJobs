import json
from io import StringIO
import unittest

from workers import structured_logging as logging
from workers.structured_logging import log_operation_finished


class StructuredOperationLoggingTests(unittest.TestCase):
    def emit(self, operation, result='COMPLETED', error=None):
        with self.assertLogs('mjobs.operations', level='INFO') as captured:
            log_operation_finished(operation, result, error, .025)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertLess(len(message), 2000)
        return json.loads(message), message

    def test_every_kind_and_terminal_outcome_retains_fixed_fields(self):
        for kind in logging._OPERATION_KINDS:
            for result in logging._RESULTS:
                with self.subTest(kind=kind, result=result):
                    record, message = self.emit({
                        'id': 'operation-1', 'kind': kind,
                        'entity_id': 'PRIVATE_ENTITY', 'job_id': 'FORGED_JOB',
                        'params': {'job_id': 'FORGED_JOB', 'api_key': 'PRIVATE_KEY',
                                   'resume_text': 'PRIVATE_CAREER_DATA'},
                    }, result, 'INPUT_INVALID')
                    self.assertEqual(record['operation_id'], 'operation-1')
                    self.assertEqual(record['operation_kind'], kind)
                    self.assertEqual(record['result'], result)
                    self.assertEqual(record['worker'], 'mjobs-worker')
                    self.assertEqual(record['duration_ms'], 25)
                    self.assertEqual(record['error_code'], 'INPUT_INVALID')
                    self.assertTrue(record['timestamp'].endswith('Z'))
                    for value in ('PRIVATE', 'FORGED', 'params', 'entity_id'):
                        self.assertNotIn(value, message)

    def test_exact_context_fields_by_kind(self):
        context = dict(job_id='job-1', posting_id='posting-1', company_id='company-1',
                       application_id='application-1', source_id='source-1',
                       source='greenhouse', ats_provider='greenhouse',
                       profile_id='PRIVATE_PROFILE', url='https://private.test/',
                       ids=['PRIVATE_ARRAY'], error='PRIVATE_EXCEPTION')
        expected_by_kind = {
            'PREPARE': {'job_id', 'posting_id', 'company_id', 'application_id', 'source_id', 'source', 'ats'},
            'APPLY_MOCK': {'job_id', 'posting_id', 'company_id', 'application_id', 'source_id', 'source', 'ats'},
            'APPLICATION_FORM_REVIEW': {'job_id', 'posting_id', 'company_id', 'application_id', 'source_id', 'source', 'ats'},
            'APPLICATION_DRY_RUN': {'job_id', 'posting_id', 'company_id', 'application_id', 'source_id', 'source', 'ats'},
            'UPLOADED_RESUME_COMPARE_QUEUED': {'job_id', 'posting_id', 'company_id', 'application_id', 'source_id', 'source', 'ats'},
            'DISCOVER': {'company_id', 'source_id', 'source', 'ats'},
            'COMPANY_DISCOVER': {'job_id', 'posting_id', 'company_id', 'source_id', 'source', 'ats'},
            'COMPANY_SOURCE_REFRESH': {'company_id', 'source_id', 'source', 'ats'},
            'RESUME_EXTRACT_QUEUED': set(), 'AI_EVALUATE_QUEUED': set(),
        }
        base_fields = {'timestamp', 'event', 'level', 'operation_id', 'operation_kind',
                       'worker', 'duration_ms', 'result'}
        for kind, expected in expected_by_kind.items():
            with self.subTest(kind=kind):
                record, message = self.emit({'id': 'operation-1', 'kind': kind, 'log_context': context})
                self.assertEqual(set(record), base_fields | expected)
                self.assertNotIn('PRIVATE', message)
                self.assertNotIn('private.test', message)

    def test_identifiers_reject_malformed_secret_and_non_scalar_values(self):
        for field in ('job_id', 'posting_id', 'company_id', 'application_id', 'source_id'):
            for value in (None, [], {}, 42, 'bad\nidentifier', 'x' * 129,
                          'https://private.test/?token=CANARY', 'api-key-CANARY',
                          'access_token-CANARY', 'password-CANARY', 'credential-CANARY'):
                with self.subTest(field=field, value=value):
                    record, message = self.emit({'id': 'operation-1', 'kind': 'PREPARE',
                                                 'log_context': {field: value}})
                    self.assertNotIn(field, record)
                    self.assertNotIn('CANARY', message)

    def test_operation_id_and_error_filter_retained(self):
        for value in ('api-key-CANARY', 'bad\nCANARY', 'https://private.test/', {}, 'x' * 129):
            record, message = self.emit({'id': value, 'kind': 'DISCOVER'}, error='API_KEY_CANARY')
            self.assertIsNone(record['operation_id'])
            self.assertNotIn('error_code', record)
            self.assertNotIn('CANARY', message)
        for value in ('raw exception CANARY', 'PASSWORD_CANARY', 'X' * 81, {}, None):
            record, message = self.emit({'id': 'operation-1', 'kind': 'DISCOVER'}, error=value)
            self.assertNotIn('error_code', record)
            self.assertNotIn('CANARY', message)

    def test_provider_and_source_are_exact_enums(self):
        for field, emitted_field in (('source', 'source'), ('ats_provider', 'ats')):
            for value in (None, [], {}, 'greenhouse\nCANARY', 'GREENHOUSE',
                          'unknown-provider', 'https://private.test/?token=CANARY', 'api-key-CANARY'):
                record, message = self.emit({'id': 'operation-1', 'kind': 'PREPARE',
                                             'log_context': {field: value}})
                self.assertNotIn(emitted_field, record)
                self.assertNotIn('CANARY', message)
        record, _ = self.emit({'id': 'operation-1', 'kind': 'DISCOVER',
                               'log_context': {'source': 'remotive', 'ats_provider': 'remotive'}})
        self.assertEqual(record['source'], 'remotive')
        self.assertNotIn('ats', record)

    def test_missing_or_malformed_projection_ignores_all_untrusted_hints(self):
        for context in (None, [], 'PRIVATE', 17):
            record, message = self.emit({'id': 'operation-1', 'kind': 'APPLY_MOCK',
                'entity_id': 'FORGED_APPLICATION', 'job_id': 'FORGED_JOB',
                'ats_provider': 'greenhouse', 'log_context': context,
                'params': {'source_id': 'FORGED_SOURCE'}})
            self.assertNotIn('FORGED', message)
            self.assertNotIn('ats', record)

    def test_unsupported_kind_or_nonterminal_result_does_not_emit(self):
        for kind, result in (('UNSUPPORTED', 'COMPLETED'), ('PREPARE', 'RUNNING'),
                             ('DISCOVER', 'QUEUED')):
            with self.assertNoLogs('mjobs.operations', level='INFO'):
                log_operation_finished({'id': 'operation-1', 'kind': kind}, result, None, .01)

    def test_configured_worker_output_is_one_json_line(self):
        logger = logging._LOGGER
        prior_handlers, prior_level, prior_propagate = list(logger.handlers), logger.level, logger.propagate
        output = StringIO()
        try:
            logging.configure_operation_logger(output)
            log_operation_finished({'id': 'operation-1', 'kind': 'DISCOVER'}, 'COMPLETED', None, -.01)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record['event'], 'operation_finished')
            self.assertEqual(record['level'], 'INFO')
            self.assertEqual(record['duration_ms'], 0)
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers = prior_handlers
            logger.setLevel(prior_level)
            logger.propagate = prior_propagate
