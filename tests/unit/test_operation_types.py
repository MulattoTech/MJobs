"""Canonical durable operation type contract."""
from importlib import import_module, util

import pytest


REQUIRED = frozenset({
    "SEARCH_SOURCE",
    "DISCOVER_COMPANY",
    "VALIDATE_COMPANY_SOURCE",
    "ENUMERATE_COMPANY",
    "PARSE_JOB",
    "DEDUPE_JOB",
    "SCORE_JOB",
    "GENERATE_RESUME",
    "GENERATE_COVER_LETTER",
    "PREPARE_APPLICATION",
    "SUBMIT_APPLICATION",
    "BACKUP",
})
RUNTIME = frozenset({
    "DISCOVER",
    "COMPANY_DISCOVER",
    "AI_EVALUATE_QUEUED",
    "COMPANY_SOURCE_REFRESH",
    "PREPARE",
    "RESUME_EXTRACT_QUEUED",
    "UPLOADED_RESUME_COMPARE_QUEUED",
    "APPLY_MOCK",
    "APPLICATION_FORM_REVIEW",
    "APPLICATION_DRY_RUN",
    "AI_EVALUATE",
    "RESUME_EXTRACT",
    "UPLOADED_RESUME_COMPARE",
    "MANUAL_AUTH",
})


def contract():
    assert util.find_spec("packages.domain.operation_types") is not None
    return import_module("packages.domain.operation_types")


def test_required_and_runtime_operation_types_form_one_immutable_contract():
    module = contract()
    from workers.operations import INLINE_KINDS, QUEUES

    assert module.REQUIRED_OPERATION_TYPES == REQUIRED
    assert module.RUNTIME_OPERATION_TYPES == RUNTIME
    assert module.RUNTIME_OPERATION_TYPES == frozenset(QUEUES) | frozenset(INLINE_KINDS)
    assert module.OPERATION_TYPES == REQUIRED | RUNTIME
    assert isinstance(module.REQUIRED_OPERATION_TYPES, frozenset)
    assert isinstance(module.RUNTIME_OPERATION_TYPES, frozenset)
    assert isinstance(module.OPERATION_TYPES, frozenset)


@pytest.mark.parametrize("operation_type", sorted(REQUIRED | RUNTIME))
def test_every_registered_operation_type_round_trips_exactly(operation_type):
    assert contract().validate_operation_type(operation_type) == operation_type


@pytest.mark.parametrize(
    "value",
    (
        "search_source",
        "UNKNOWN_PRIVATE_SENTINEL",
        " SEARCH_SOURCE",
        "SEARCH_SOURCE ",
        None,
        7,
        True,
        [],
        {},
    ),
)
def test_invalid_operation_type_uses_one_redacted_error(value):
    with pytest.raises(ValueError) as raised:
        contract().validate_operation_type(value)

    assert str(raised.value) == "OPERATION_TYPE_INVALID"
    assert "UNKNOWN_PRIVATE_SENTINEL" not in repr(raised.value)
