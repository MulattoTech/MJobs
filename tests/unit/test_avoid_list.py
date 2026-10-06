import unittest

from packages.domain.profile import JobPreferences
from services.documents.avoid_list import find_avoided_term, validate_content


class AvoidListTests(unittest.TestCase):
    def test_whole_terms_casefold_possessives_unicode_and_partial_words(self):
        terms = ['North Star', 'Café', 'AI']
        self.assertEqual(find_avoided_term("The NORTH STAR's old project", terms), 'North Star')
        self.assertEqual(find_avoided_term('Cafe\u0301 work', terms), 'Café')
        self.assertEqual(find_avoided_term('AI-powered tools', terms), 'AI')
        self.assertIsNone(find_avoided_term('Northstar cafe chair workflow', terms))
        self.assertIsNone(find_avoided_term('xAI and AI_helper', terms))

    def test_block_reason_names_only_the_term_and_output_path(self):
        with self.assertRaises(ValueError) as raised:
            validate_content(
                'The restricted client name appears here; unrelated private text stays hidden',
                {'avoid_terms': ['Restricted Client']}, 'cover letter')
        self.assertEqual(raised.exception.code, 'AVOID_LIST_BLOCKED')
        self.assertIn('Restricted Client', str(raised.exception))
        self.assertIn('cover letter', str(raised.exception))
        self.assertNotIn('unrelated private text', str(raised.exception))

    def test_preferences_normalize_and_deduplicate_terms_and_bound_size(self):
        preferences = JobPreferences(avoid_terms=['  North Star  ', 'north star', 'Résumé'])
        self.assertEqual(preferences.avoid_terms, ['North Star', 'Résumé'])
        with self.assertRaises(ValueError):
            JobPreferences(avoid_terms=['   '])
        with self.assertRaises(ValueError):
            JobPreferences(avoid_terms=['x' * 121])
        with self.assertRaises(ValueError):
            JobPreferences(avoid_terms=['term'] * 51)


if __name__ == '__main__':
    unittest.main()
