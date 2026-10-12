"""Document history uses canonical persisted identities before filesystem access."""
import unittest
from unittest.mock import MagicMock, patch
from services.documents.errors import PreparationError
from services.documents.history import application_documents


class DocumentHistoryPathTests(unittest.TestCase):
    def test_invalid_persisted_application_component_rejected_before_filesystem_access(self):
        for identifier in ('.', '..', '../sibling', 'folder/other', r'folder\other',
                           'C:escape', '/absolute', 'nul\x00byte', ''):
            with self.subTest(identifier=identifier):
                store = MagicMock()
                store.application.return_value = {'id': identifier, 'profile_id': 'owner', 'job_id': 'job'}
                store.profile.return_value = {'preferences': {}}
                store.engine.connect.return_value.__enter__.return_value.execute.return_value.mappings.return_value = []
                with patch('services.documents.history.Path') as paths, patch(
                        'services.documents.answer_story_citations.answer_story_citations_by_document', return_value={}):
                    with self.assertRaises(PreparationError) as raised:
                        application_documents(store, 'requested-id', output_dir='/synthetic/workspace')
                self.assertEqual(raised.exception.code, 'DOCUMENT_CONTENT_MISMATCH')
                paths.assert_not_called()
