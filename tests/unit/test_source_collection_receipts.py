"""Bounds and completeness of native batch receipts, not acceptance claims."""
from unittest.mock import patch

import pytest

from services.discovery import source_collection_receipts as receipts


def test_receipt_distinguishes_empty_complete_partial_rejected_and_duplicate_batches():
    cases = [
        (True, 0, 0, [], True),
        (False, 1, 0, [{'posting_id': 'one'}], False),
        (True, 1, 1, [{'posting_id': 'one'}], False),
        (True, 2, 0, [{'posting_id': 'one'}], False),
    ]
    for complete, accepted, rejected, manifest, expected in cases:
        result = receipts.bounded_receipt(dict(provider_complete_inventory=complete,
            accepted_count=accepted, rejected_count=rejected), manifest)
        assert result['inventory_complete'] is expected
        assert result['status'] == 'AVAILABLE'
        assert result['manifest'] == manifest
        assert result['sha256'] == receipts.canonical_digest({k: v for k, v in result.items() if k != 'sha256'})


@pytest.mark.parametrize('limit', ['MAX_ENTRIES', 'MAX_BYTES'])
def test_capacity_refusal_has_no_truncated_positive_manifest(limit):
    with patch.object(receipts, limit, 0):
        result = receipts.bounded_receipt(dict(provider_complete_inventory=True,
            accepted_count=1, rejected_count=0), [{'posting_id': 'one'}])
    assert result['status'] == 'UNAVAILABLE'
    assert result['reason'] == receipts.CAPACITY_REASON
    assert result['manifest'] is result['manifest_sha256'] is None
    assert result['inventory_complete'] is False
    assert result['accepted_count'] == 1
