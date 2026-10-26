"""Boundary regressions for fail-closed education requirement parsing."""
import re
import unittest

from services.discovery.search_semantics import parse_education_requirement


class EducationRequirementBoundsTests(unittest.TestCase):
    _STATEMENT = "Bachelor's degree required."

    def _classification(self, description):
        return parse_education_requirement(description)["classification"]

    def test_complete_statement_survives_just_under_length_limit(self):
        description = self._STATEMENT + (" " * (65535 - len(self._STATEMENT)))

        self.assertEqual(len(description), 65535)
        self.assertEqual(self._classification(description), "BACHELORS_REQUIRED")

    def test_complete_statement_survives_just_under_token_limit(self):
        statement_tokens = re.findall(r"and/or|\w+(?:'\w+)?|[^\w\s]", self._STATEMENT, re.I)
        description = self._STATEMENT + (" filler" * (4095 - len(statement_tokens)))
        tokens = re.findall(r"and/or|\w+(?:'\w+)?|[^\w\s]", description, re.I)

        self.assertEqual(len(tokens), 4095)
        self.assertEqual(self._classification(description), "BACHELORS_REQUIRED")

    def test_complete_statement_survives_maximum_supported_nesting(self):
        description = self._STATEMENT + " " + ("(" * 8) + "filler" + (")" * 8)

        self.assertEqual(self._classification(description), "BACHELORS_REQUIRED")

    def test_over_length_input_does_not_return_a_partial_classification(self):
        description = self._STATEMENT + (" " * (65537 - len(self._STATEMENT)))

        self.assertEqual(self._classification(description), "NONE_SPECIFIED")

    def test_over_token_input_does_not_return_a_partial_classification(self):
        statement_tokens = re.findall(r"and/or|\w+(?:'\w+)?|[^\w\s]", self._STATEMENT, re.I)
        description = self._STATEMENT + (" filler" * (4097 - len(statement_tokens)))

        self.assertEqual(self._classification(description), "NONE_SPECIFIED")

    def test_over_depth_input_does_not_return_a_partial_classification(self):
        description = self._STATEMENT + " " + ("(" * 9) + "filler" + (")" * 9)

        self.assertEqual(self._classification(description), "NONE_SPECIFIED")

    def test_truncated_degree_subject_cannot_borrow_neighbor_requirement(self):
        truncated = "Bachelor's degree in Computer Science or Engineering, Python"
        neighboring_requirement = "Bachelor's degree in Computer Science or Engineering; Python required."

        self.assertEqual(self._classification(truncated), "NONE_SPECIFIED")
        self.assertEqual(self._classification(neighboring_requirement), "NONE_SPECIFIED")


if __name__ == "__main__":
    unittest.main()
