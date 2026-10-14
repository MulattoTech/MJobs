"""Project board audit must catch drift without falsifying acceptance."""
import unittest

from tools.verification.project_board_alignment import REPOSITORY, audit


def issue(number=10, state="OPEN", priority="P1"):
    return {"number": number, "state": state, "labels": [{"name": f"priority:{priority}"}]}


def card(number=10, status="Todo", acceptance="PARTIAL", priority="P1"):
    return {"content": {"type": "Issue", "number": number, "repository": REPOSITORY},
            "title": "Synthetic card", "status": status,
            "priority": priority, "acceptance": acceptance}


class ProjectBoardAlignmentTests(unittest.TestCase):
    def test_aligned_open_issue_stays_todo(self):
        result = audit([card()], [issue()])
        self.assertTrue(result["aligned"])
        self.assertEqual(result["warnings"], [])

    def test_missing_card_is_error(self):
        result = audit([], [issue()])
        self.assertFalse(result["aligned"])
        self.assertEqual(result["errors"][0]["kind"], "issue_missing_from_project")

    def test_priority_mismatch_is_error(self):
        result = audit([card(priority="P0")], [issue(priority="P1")])
        self.assertEqual(result["errors"][0]["kind"], "priority_mismatch")

    def test_done_open_issue_is_error(self):
        result = audit([card(status="Done")], [issue()])
        self.assertEqual(result["errors"][0]["kind"], "delivery_status_mismatch")

    def test_open_pass_is_warning_not_auto_close(self):
        result = audit([card(acceptance="PASS")], [issue()])
        self.assertTrue(result["aligned"])
        self.assertEqual(result["warnings"][0]["kind"], "open_with_pass_acceptance")

    def test_closed_partial_is_warning_not_promotion(self):
        result = audit([card(status="Done", acceptance="PARTIAL")], [issue(state="CLOSED")])
        self.assertTrue(result["aligned"])
        self.assertEqual(result["warnings"][0]["kind"], "closed_without_pass_acceptance")

    def test_duplicate_board_card_is_error(self):
        result = audit([card(), card()], [issue()])
        self.assertEqual(result["errors"][0]["kind"], "duplicate_project_card")

    def test_non_issue_card_is_error(self):
        result = audit([{"title": "draft", "content": {"type": "DraftIssue"}}], [])
        self.assertFalse(result["aligned"])


if __name__ == "__main__":
    unittest.main()
