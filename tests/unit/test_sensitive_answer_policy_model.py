import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from packages.domain.profile import JobPreferences, SensitiveAnswerPolicy


class SensitiveAnswerPolicyModelTests(unittest.TestCase):
    def test_omitted_policies_resolve_to_review_required(self):
        preferences = JobPreferences()

        self.assertEqual(preferences.sensitive_answer_policies, {})
        self.assertEqual(
            preferences.sensitive_answer_policy_for('RACE_ETHNICITY').mode,
            'ALWAYS_REQUIRE_REVIEW',
        )
        self.assertEqual(
            JobPreferences.model_validate({}).sensitive_answer_policy_for('RELIGION').mode,
            'ALWAYS_REQUIRE_REVIEW',
        )

    def test_configured_response_is_explicit_trimmed_and_bounded(self):
        preferences = JobPreferences.model_validate({
            'sensitive_answer_policies': {
                'VETERAN_STATUS': {
                    'mode': 'USE_CONFIGURED_RESPONSE',
                    'response': '  Prefer not to disclose  ',
                },
            },
        })

        self.assertEqual(
            preferences.sensitive_answer_policies['VETERAN_STATUS'].response,
            'Prefer not to disclose',
        )
        with self.assertRaises(ValidationError):
            SensitiveAnswerPolicy(mode='USE_CONFIGURED_RESPONSE')
        with self.assertRaises(ValidationError):
            SensitiveAnswerPolicy(mode='USE_CONFIGURED_RESPONSE', response='   ')
        with self.assertRaises(ValidationError):
            SensitiveAnswerPolicy(mode='USE_CONFIGURED_RESPONSE', response='x' * 501)

    def test_response_is_forbidden_for_non_configured_modes(self):
        for mode in ('PREFER_NOT_TO_ANSWER', 'ALWAYS_REQUIRE_REVIEW'):
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                SensitiveAnswerPolicy(mode=mode, response='Prefer not to disclose')

    def test_unknown_categories_modes_and_fields_are_rejected(self):
        for policies in (
            {'UNKNOWN': {'mode': 'ALWAYS_REQUIRE_REVIEW'}},
            {'RELIGION': {'mode': 'AUTOMATICALLY_ANSWER'}},
            {'RACE_ETHNICITY': {'mode': 'ALWAYS_REQUIRE_REVIEW', 'extra': True}},
        ):
            with self.subTest(policies=policies), self.assertRaises(ValidationError):
                JobPreferences.model_validate({'sensitive_answer_policies': policies})

    def test_sensitive_policies_persist_with_their_own_profile(self):
        from packages.persistence.store import Store

        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'profiles.db').as_posix()}")
            try:
                store.migrate()
                for profile_id, name, mode in (
                    ('profile-a', 'Profile A', 'PREFER_NOT_TO_ANSWER'),
                    ('profile-b', 'Profile B', 'USE_CONFIGURED_RESPONSE'),
                ):
                    preferences = {
                        'sensitive_answer_policies': {
                            'RELIGION': {'mode': mode},
                        },
                    }
                    if mode == 'USE_CONFIGURED_RESPONSE':
                        preferences['sensitive_answer_policies']['RELIGION']['response'] = 'No response'
                    store.save_profile({
                        'id': profile_id,
                        'name': name,
                        'email': f'{profile_id}@example.test',
                        'facts': [],
                        'preferences': preferences,
                    })

                profile_a = store.profile('profile-a')
                profile_b = store.profile('profile-b')
                self.assertEqual(
                    profile_a['preferences']['sensitive_answer_policies']['RELIGION']['mode'],
                    'PREFER_NOT_TO_ANSWER',
                )
                self.assertEqual(
                    profile_b['preferences']['sensitive_answer_policies']['RELIGION'],
                    {'mode': 'USE_CONFIGURED_RESPONSE', 'response': 'No response'},
                )
            finally:
                store.engine.dispose()


if __name__ == '__main__':
    unittest.main()
