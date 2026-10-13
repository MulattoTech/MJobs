"""Run source quotation durability on the guarded disposable PostgreSQL fixture."""
import unittest
from tests.integration import test_profile_ai_credentials_postgres as postgres
from tests.integration import test_ai_source_quotes as contract


@unittest.skipUnless(postgres.POSTGRES_URL, 'Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL')
class AISourceQuotePostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.pg = postgres.ProfileAICredentialPostgreSQLTests()
        self.addCleanup(self.pg.doCleanups)
        self.pg.setUp()
        self.case = contract.AISourceQuoteTests()
        self.case.fixture = self.pg.case
        self.case.store, self.case.client = self.pg.case.store, self.pg.case.client
        self.case.quotes = [{'field': 'description', 'quote': 'Python SQL synthetic private sentinel.'}]
        self.pg.case.response_body['job_evidence'] = self.case.quotes

    def test_worker_quotes_reload_and_stale_posting(self):
        self.case.test_real_worker_quotes_survive_reload_and_job_edit_without_recall()

    def test_invented_quote_blocks_publication(self):
        self.case.test_invented_quote_blocks_publication_and_is_not_logged()

    def test_legacy_unknown(self):
        self.case.test_legacy_result_remains_unknown()

    def test_tampered_checkpoint(self):
        self.case.test_tampered_checkpoint_cannot_claim_verified_quotes()

    def test_restart_without_duplicate_call(self):
        self.case.test_staged_quote_response_recovers_without_a_second_provider_call()

    def test_invalid_second_candidate_never_publishes_partial_batch(self):
        self.case.test_invalid_second_candidate_never_publishes_a_partial_batch()
