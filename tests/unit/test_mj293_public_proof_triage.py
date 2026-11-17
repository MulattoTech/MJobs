"""Safety and classification checks for offline MJ-REQ-292 public evidence."""
import pytest

from tools.verification.mj293_public_proof_triage import summarize_public_observations


def test_terminal_children_are_counted_without_identifiers():
    data = {"collection_state": "COMPLETED", "child_discovery": {
        "enrolled_sources": [], "children": [
            {"state": "BLOCKED", "reason": "COMPANY_DOMAIN_CORROBORATION_REQUIRED",
             "company_id": "private-123", "url": "https://private.example"},
            {"state": "BLOCKED", "reason": "COMPANY_DOMAIN_CORROBORATION_REQUIRED"},
        ]}}
    result = summarize_public_observations(data)
    assert result["child_count"] == 2
    assert result["reasons"] == {"COMPANY_DOMAIN_CORROBORATION_REQUIRED": 2}
    assert result["enrolled_sources_present"] is False
    assert "private" not in str(result)
    assert "example" not in str(result)


def test_untrusted_codes_and_states_are_not_reflected():
    result = summarize_public_observations({"collection_state": "secret", "child_discovery": {
        "children": [{"state": "user-private-state", "reason": "user-private-reason"}]}})
    assert result["states"] == {"OTHER": 1}
    assert result["reasons"] == {"OTHER": 1}
    assert result["public_collection_state"] == "UNKNOWN"


@pytest.mark.parametrize("data", [None, [], {}, {"child_discovery": {"children": "wrong"}},
                                  {"child_discovery": {"children": [{}]}},
                                  {"child_discovery": {"children": [1]}}])
def test_invalid_input_fails_closed(data):
    with pytest.raises(ValueError):
        summarize_public_observations(data)
