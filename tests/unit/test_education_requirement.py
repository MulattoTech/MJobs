"""Education claims must retain their own strength and polarity."""
import unittest
import re

from services.discovery.search_semantics import parse_education_requirement


class EducationRequirementTests(unittest.TestCase):
    def test_completed_subject_never_borrows_arbitrary_unrelated_qualifiers(self):
        subjects = ('Python', 'Zorblatt tooling', 'incident response', 'French')
        separators = (', ', ', and ', ': ', ' — ')
        for conjunction in ('or', 'and'):
            fields = 'Computer Science ' + conjunction + ' Engineering'
            contexts = (
                "Bachelor's degree in " + fields,
                'Bachelor’s degree (' + fields + ')',
                'Degree [Computer Science [Systems, Networks] ' + conjunction + ' Engineering]',
            )
            for context in contexts:
                for separator in separators:
                    for subject in subjects:
                        for qualifier in ('required', 'preferred'):
                            text = context + separator + subject + ' ' + qualifier + '.'
                            with self.subTest(text=text):
                                result = parse_education_requirement(text)
                                self.assertEqual(result['classification'], 'NONE_SPECIFIED')
                                self.assertIsNone(result['raw'])

    def test_terminal_subject_still_accepts_its_own_qualification_and_alternative(self):
        cases = (
            ("Bachelor's degree in Computer Science or Engineering required, Python preferred.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science or Engineering preferred, Python required.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree in Computer Science or Engineering, required.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science or Engineering, preferred.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree in Computer Science or Engineering, or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            ("Bachelor's degree (Computer Science [Systems or Networking] or Engineering) required.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science or Engineering: required.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science or Engineering — preferred.", 'BACHELORS_PREFERRED'),
            ("Required: a bachelor's degree in Computer Science or Engineering, Zorblatt tooling preferred.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science or Engineering, not required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree in Computer Science or Engineering, not required, but preferred.", 'BACHELORS_PREFERRED'),
        )
        for text, category in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_education_requirement(text)['classification'], category)

    def test_positive_subject_corpus_retains_base_categories_under_list_wrappers(self):
        # Frozen positive recognition contract from main37ea5e95, before this PR.
        # Compare the independent span parser with that baseline on ordinary
        # unnegated claims; negative controls below deliberately improve it.
        baseline = (
            ('BACHELORS_OR_EQUIVALENT_EXPERIENCE', r"\bbachelor(?:'s|s)?(?:\s+degree)?\b[^.;\n]{0,80}\b(?:or|and/or)\b[^.;\n]{0,60}\bequivalent\s+(?:experience|combination)\b"),
            ('BACHELORS_PREFERRED', r"\bbachelor(?:'s|s)?(?:\s+degree)?\b[^.;\n]{0,80}\bpreferred\b"),
            ('BACHELORS_REQUIRED', r"\bbachelor(?:'s|s)?(?:\s+degree)?\b[^.;\n]{0,80}\b(?:required|mandatory)\b"),
            ('DEGREE_PREFERRED', r"\b(?:college\s+|university\s+)?degree\b[^.;\n]{0,60}\bpreferred\b"),
        )
        descriptors = (
            '', ' in Computer Science',
            ' in Computer Science, Engineering',
            ' in Computer Science, Engineering, or related field',
            ' (Computer Science, Engineering, or related field)',
            ' (Computer Science, Engineering)',
            ' [Computer Science, Engineering]',
            ' {Computer Science, Engineering}',
            ' (Computer Science [Systems, Engineering])',
            ' Computer Science, Engineering',
            ', Computer Science, Engineering',
            ' in Computer Science and Engineering',
            ' in Computer Science or Engineering',
            ' — Computer Science, Engineering —',
            ': Computer Science, Engineering',
        )
        categories = (
            (' required', 'BACHELORS_REQUIRED'),
            (' mandatory', 'BACHELORS_REQUIRED'),
            (' preferred', 'BACHELORS_PREFERRED'),
            (' or equivalent experience required', 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
        )
        for prefix in ("Bachelor's degree", 'Bachelors degree', 'Bachelor degree'):
            for descriptor in descriptors:
                for suffix, category in categories:
                    original = prefix + descriptor + suffix
                    expected = next((label for label, pattern in baseline if re.search(pattern, original, re.I)), 'NONE_SPECIFIED')
                    self.assertEqual(expected, category, original)
                    for text in dict.fromkeys((original, original.replace("'", '’'))):
                        with self.subTest(text=text):
                            result = parse_education_requirement(text)
                            self.assertEqual(result['classification'], expected)
                            self.assertEqual(result['raw'], ' '.join(text.split()))
        for descriptor in descriptors:
            text = 'Degree' + descriptor + ' preferred'
            with self.subTest(text=text):
                expected = next((label for label, pattern in baseline if re.search(pattern, text, re.I)), 'NONE_SPECIFIED')
                self.assertEqual(expected, 'DEGREE_PREFERRED')
                self.assertEqual(parse_education_requirement(text)['classification'], expected)

    def test_bracketed_subjects_preserve_polarity_and_unrelated_subject_boundaries(self):
        cases = (
            ("Bachelor's degree (Computer Science, Engineering) not required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science, Engineering), Python required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree [Computer Science, Engineering], security clearance required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree, and Python required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree, but Python required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science, Engineering), and security clearance required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science, Engineering) not required, but preferred.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree (Computer Science; Engineering) required.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree (Computer Science [Systems, Engineering]) preferred.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree (Computer Science, Engineering), or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            ("Bachelor's degree (Computer Science, Engineering, or equivalent experience) required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            ("Bachelor's degree required (or equivalent experience) not accepted.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree (Computer Science, Engineering, or equivalent experience) not accepted.", 'NONE_SPECIFIED'),
            ("Bachelor's degree optional, Python required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science, Engineering) optional, Python required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (security clearance required).", 'NONE_SPECIFIED'),
            ("Bachelor's degree (not required; security clearance required).", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science preferred; security clearance required).", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree (required).", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science, Engineering not required, Python mandatory.", 'NONE_SPECIFIED'),
            ("Bachelor's degree (Computer Science, Engineering) required; Python preferred.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree (Computer Science, Engineering required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree [Computer Science, Engineering) required.", 'NONE_SPECIFIED'),
        )
        for text, category in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_education_requirement(text)['classification'], category)

    def test_supported_categories_negation_and_clause_conflicts(self):
        cases = (
            ("Bachelor's required", "BACHELORS_REQUIRED"),
            ("Bachelor’s degree is mandatory.", "BACHELORS_REQUIRED"),
            ("Required: a bachelor's degree.", "BACHELORS_REQUIRED"),
            ("We require a bachelor's degree.", "BACHELORS_REQUIRED"),
            ("Bachelor‘s degree preferred.", "BACHELORS_PREFERRED"),
            ("Bachelorʼs degree — preferred.", "BACHELORS_PREFERRED"),
            ("Bachelor's degree in computer science preferred.", "BACHELORS_PREFERRED"),
            ("Bachelor's or equivalent experience", "BACHELORS_OR_EQUIVALENT_EXPERIENCE"),
            ("Bachelor's degree and/or equivalent experience required.", "BACHELORS_OR_EQUIVALENT_EXPERIENCE"),
            ("Bachelor's degree required or equivalent experience.", "BACHELORS_OR_EQUIVALENT_EXPERIENCE"),
            ("College degree preferred", "DEGREE_PREFERRED"),
            ("University degree preferred.", "DEGREE_PREFERRED"),
            ("No degree specified", "NONE_SPECIFIED"),
            ("Python experience required.", "NONE_SPECIFIED"),
            (None, "NONE_SPECIFIED"),
            ("Bachelor's degree", "NONE_SPECIFIED"),
            ("Bachelor's degree not required.", "NONE_SPECIFIED"),
            ("Bachelor's degree is not mandatory.", "NONE_SPECIFIED"),
            ("Bachelor's degree isn't required.", "NONE_SPECIFIED"),
            ("Bachelor's degree isn’t required.", "NONE_SPECIFIED"),
            ("Bachelor's degree is not necessarily required.", "NONE_SPECIFIED"),
            ("No bachelor's degree required.", "NONE_SPECIFIED"),
            ("Not required: a bachelor's degree.", "NONE_SPECIFIED"),
            ("Bachelor's degree not preferred.", "NONE_SPECIFIED"),
            ("College degree not preferred.", "NONE_SPECIFIED"),
            ("Bachelor's degree is not required or mandatory.", "NONE_SPECIFIED"),
            ("Bachelor's degree is not required and mandatory.", "NONE_SPECIFIED"),
            ("No bachelor's degree required, but preferred.", "BACHELORS_PREFERRED"),
            ("No degree required, but preferred.", "DEGREE_PREFERRED"),
            ("Bachelor's degree not required, but preferred.", "BACHELORS_PREFERRED"),
            ("Bachelor's degree preferred, not required.", "BACHELORS_PREFERRED"),
            ("Bachelor's degree required, not preferred.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree not required; Python required.", "NONE_SPECIFIED"),
            ("Bachelor's degree not required and Python required.", "NONE_SPECIFIED"),
            ("Bachelor's degree not required, security clearance mandatory.", "NONE_SPECIFIED"),
            ("Bachelor's degree preferred; security clearance required.", "BACHELORS_PREFERRED"),
            ("Bachelor's degree required. College degree preferred.", "BACHELORS_REQUIRED"),
            ("College degree preferred. Bachelor's degree required.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree preferred. Bachelor's degree required.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree not required. Bachelor's degree mandatory.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree required; equivalent experience not accepted.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree required or equivalent experience not accepted.", "BACHELORS_REQUIRED"),
            ("Bachelor's degree required. Bachelor's or equivalent experience.", "BACHELORS_REQUIRED"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                result = parse_education_requirement(text)
                self.assertEqual(result['classification'], expected)
                if expected == 'NONE_SPECIFIED':
                    self.assertIsNone(result['raw'])
                else:
                    self.assertIn(result['raw'], ' '.join(text.split()))

    def test_original_typography_is_retained_as_evidence(self):
        text = 'Bachelor’s degree preferred'
        self.assertEqual(parse_education_requirement(text), {
            'classification': 'BACHELORS_PREFERRED', 'raw': text,
        })

    def test_field_lists_retain_qualifiers_without_absorbing_other_requirements(self):
        cases = (
            ("Bachelor's degree in Computer Science, Engineering, or a related field required.", 'BACHELORS_REQUIRED'),
            ("Bachelor’s degree in Computer Science, Engineering, or a related field preferred.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree in Computer Science, Engineering, or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            ("Bachelor's degree, or equivalent experience required.", 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'),
            ("Degree in Computer Science, Engineering, or a related field preferred.", 'DEGREE_PREFERRED'),
            ("Bachelor's degree in Computer Science and Engineering required.", 'BACHELORS_REQUIRED'),
            ("Bachelor's degree in Computer Science, Engineering, and Mathematics preferred.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree in Computer Science, Engineering, or a related field not required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree in Computer Science, Engineering, or a related field preferred, Python required.", 'BACHELORS_PREFERRED'),
            ("Bachelor's degree in Computer Science, Engineering, or a related field not required, security clearance mandatory.", 'NONE_SPECIFIED'),
            ("Bachelor's degree in Computer Science, security clearance required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree in Computer Science or security clearance required.", 'NONE_SPECIFIED'),
            ("Bachelor's degree, Python required.", 'NONE_SPECIFIED'),
        )
        for text, category in cases:
            with self.subTest(text=text):
                result = parse_education_requirement(text)
                self.assertEqual(result['classification'], category)
                if category != 'NONE_SPECIFIED':
                    self.assertEqual(result['raw'], text.rstrip('.'))
