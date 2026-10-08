"""Durable document preparation rejects reviewed rewrites that drop named credentials."""
import unittest

from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.errors import PreparationError
from services.documents.generate import prepare
from services.documents.history import application_documents
from sqlalchemy import update
from packages.persistence import models as m
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumeTailoringFactsIntegrationTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def test_named_credential_drop_does_not_create_a_tailored_resume_version(self):
        experience = dict(PROFILE['facts'][0], id='experience', kind='experience',
            text='Supported Cisco CCNA routing for 10 sites since 2020.')
        self.store.save_profile(dict(PROFILE, facts=[experience]))
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(skills=['Routing']))
        application_id = prepare(self.store, job['id'], 'fixture', self.root)
        before = application_documents(self.store, application_id)

        with self.assertRaises(PreparationError) as raised:
            prepare(self.store, job['id'], 'fixture', self.root, tailor_resume=True,
                tailor_fact_ids=['experience'], tailor_fact_rewrites=[{
                    'fact_id': 'experience',
                    'text': 'Supported network routing for 10 sites since 2020.',
                    'reviewed': True,
                }])

        self.assertEqual(raised.exception.code, 'RESUME_REPHRASE_FACTS_CHANGED')
        after = application_documents(self.store, application_id)
        self.assertEqual([row['id'] for row in after], [row['id'] for row in before])
        self.assertFalse(any(row['kind'] == 'resume' and row['version'] == 2 for row in after))
        self.assertEqual(self.store.profile('fixture')['facts'][0]['text'], experience['text'])


if __name__ == '__main__':
    unittest.main()
