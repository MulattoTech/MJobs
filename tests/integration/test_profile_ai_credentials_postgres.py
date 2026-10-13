"""Profile credential routing and recovery on disposable PostgreSQL."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_durable_ai_evaluation as durable
from tests.integration import test_profile_ai_credentials as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url

POSTGRES_URL = os.environ.get('MJOBS_AI_EVALUATION_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL')
class ProfileAICredentialPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = 'mjobs_profile_ai_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.cleanup_schema)
        database_url = url.update_query_dict({'options': '-csearch_path=' + self.schema +
            ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)

        class TestStore(Store):
            def __init__(self, ignored):
                super().__init__(database_url)

            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)

        store_patch = patch.object(durable, 'Store', TestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case = contract.ProfileAICredentialTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.case.url = database_url
        self.enterContext(patch.dict(os.environ, {'MJOBS_DATABASE_URL': database_url}))

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_saved_key_reaches_real_router_at_fixed_origin_and_stays_private(self):
        self.case.test_saved_key_reaches_real_router_at_fixed_origin_and_stays_private()

    def test_rotation_before_worker_blocks_without_fallback_or_call(self):
        self.case.test_rotation_before_worker_blocks_without_fallback_or_call()

    def test_deletion_before_worker_blocks_without_environment_fallback(self):
        self.case.test_deletion_before_worker_blocks_without_environment_fallback()

    def test_unavailable_encryption_key_fails_before_call_started(self):
        self.case.test_unavailable_encryption_key_fails_before_call_started()

    def test_rotation_during_response_prevents_publication(self):
        self.case.test_rotation_during_response_prevents_publication()

    def test_rotation_after_preview_requires_new_explicit_plan(self):
        self.case.test_rotation_after_preview_requires_new_explicit_plan()

    def test_each_profile_uses_only_its_own_saved_key(self):
        self.case.test_each_profile_uses_only_its_own_saved_key()

    def test_saved_key_is_never_forwarded_to_a_redirect(self):
        self.case.test_saved_key_is_never_forwarded_to_a_redirect()

    def test_rotation_after_staging_is_fenced_in_completion_transaction(self):
        self.case.test_rotation_after_staging_is_fenced_in_completion_transaction()

    def test_pending_worker_restart_keeps_binding_and_calls_once(self):
        self.case.test_pending_worker_restart_keeps_binding_and_calls_once()

    def test_runtime_admission_does_not_switch_to_a_later_saved_key(self):
        self.case.test_runtime_admission_does_not_switch_to_a_later_saved_key()

    def test_disabled_provider_never_resolves_a_saved_secret(self):
        self.case.test_disabled_provider_never_resolves_a_saved_secret()

    def test_separate_worker_process_decrypts_only_the_bound_saved_key(self):
        self.case.test_separate_worker_process_decrypts_only_the_bound_saved_key()

    def test_rotation_during_admission_rolls_back_the_outbox(self):
        self.case.test_rotation_during_admission_rolls_back_the_outbox()

    def test_other_profiles_ciphertext_cannot_be_decrypted_as_this_owner(self):
        self.case.test_other_profiles_ciphertext_cannot_be_decrypted_as_this_owner()

    def test_legacy_snapshot_keeps_runtime_source_without_backfilling_a_key(self):
        self.case.test_legacy_snapshot_keeps_runtime_source_without_backfilling_a_key()

