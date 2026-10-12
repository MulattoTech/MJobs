"""Standalone egress authorization revalidates proof without transaction locks."""
import unittest
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.requests import approve_discovery_request, authorize_discovery_request
from tests.integration.test_reviewed_employer_identity_continuity import (
    ReviewedEmployerIdentityContinuityTests,
)


class ReviewedEmployerStandaloneAuthorizationTests(unittest.TestCase):
    def test_standalone_confirmed_authorization_skips_transaction_lock_helpers(self):
        case = ReviewedEmployerIdentityContinuityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        request = case.collect()
        queued = approve_discovery_request(
            case.store, request['id'], 'https://employer.example/', request['revision'],
            confirm_company_identity=True,
        )
        with case.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued['operation_id'])).mappings().one())
            authorized = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()

        with patch('services.company_discovery.requests.lock_company_ingestion',
                   side_effect=AssertionError('standalone checks must not acquire ingestion locks')), \
             patch('services.company_discovery.requests.lock_domain_evidence',
                   side_effect=AssertionError('standalone checks must not acquire domain locks')), \
             patch('services.company_discovery.requests.lock_company_source_hosts',
                   side_effect=AssertionError('standalone checks must not acquire host locks')):
            result = authorize_discovery_request(case.store, operation)

        self.assertEqual(result, {
            'company_id': case.company_id,
            'authorization': 'USER_CONFIRMED_DOMAIN',
            'source_authorization': {
                'kind': 'USER_CONFIRMED_DOMAIN', 'request_id': request['id'],
                'revision': request['revision'] + 1, 'operation_id': operation['id'],
                'website': 'https://employer.example/',
                'proof_sha256': authorized['domain_evidence'][0]['sha256'],
            },
        })


if __name__ == '__main__':
    unittest.main()
