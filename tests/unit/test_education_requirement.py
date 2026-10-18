"""Education claims must retain their own strength and polarity."""
import unittest

from services.discovery.search_semantics import parse_education_requirement


class EducationRequirementTests(unittest.TestCase):
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
