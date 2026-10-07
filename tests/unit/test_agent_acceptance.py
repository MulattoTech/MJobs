import unittest

from services.agents.acceptance import CollectionContract, validate_collection_output


class CollectionOutputAcceptanceTests(unittest.TestCase):
    def test_validates_count_shape_and_expected_tail(self):
        contract = CollectionContract(
            items_key="items",
            count_key="count",
            expected_count=2,
            tail_key="id",
            expected_tail="job-2",
            required_item_fields=("id", "title"),
        )

        result = validate_collection_output({
            "items": [{"id": "job-1", "title": "Admin"},
                      {"id": "job-2", "title": "Engineer"}],
            "count": 2,
        }, contract)

        self.assertEqual(result.code, "OUTPUT_VALID")
        self.assertEqual(result.observed_count, 2)

    def test_empty_output_distinguishes_unknown_from_verified_zero(self):
        unknown = validate_collection_output({"items": [], "count": None},
                                             CollectionContract(items_key="items", count_key="count"))
        verified_zero = validate_collection_output({"items": [], "count": 0},
                                                   CollectionContract(items_key="items", count_key="count",
                                                                      expected_count=0))
        unverified_empty = validate_collection_output({"items": []},
                                                      CollectionContract(items_key="items"))

        self.assertEqual(unknown.code, "COUNT_UNKNOWN")
        self.assertEqual(verified_zero.code, "OUTPUT_VALID")
        self.assertEqual(unverified_empty.code, "OUTPUT_EMPTY")

    def test_reports_unknown_count_instead_of_accepting_partial_output(self):
        contract = CollectionContract(items_key="items", count_key="count", expected_count=2)

        result = validate_collection_output({"items": [{"id": "job-1"}], "count": None}, contract)

        self.assertEqual(result.code, "COUNT_UNKNOWN")
        self.assertIsNone(result.observed_count)

    def test_rejects_count_tail_and_shape_mismatches(self):
        contract = CollectionContract(items_key="items", count_key="count", expected_count=2,
                                      tail_key="id", expected_tail="job-2",
                                      required_item_fields=("title",))
        cases = [
            ({"items": [{"title": "Admin"}], "count": 1}, "COUNT_MISMATCH"),
            ({"items": [{"id": "job-1", "title": "Admin"},
                         {"id": "job-x", "title": "Engineer"}], "count": 2}, "TAIL_MISMATCH"),
            ({"items": [{"id": "job-1"}, {"id": "job-2"}], "count": 2}, "STRUCTURE_INVALID"),
        ]
        for output, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                self.assertEqual(validate_collection_output(output, contract).code, expected_code)

    def test_rejects_malformed_count_and_collection_shape(self):
        contract = CollectionContract(items_key="items", count_key="count")
        self.assertEqual(validate_collection_output({"items": {}, "count": 0}, contract).code,
                         "STRUCTURE_INVALID")
        self.assertEqual(validate_collection_output({"items": [], "count": True}, contract).code,
                         "STRUCTURE_INVALID")


if __name__ == "__main__":
    unittest.main()
