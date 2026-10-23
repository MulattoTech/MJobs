"""Canonical external-operation error categories stay finite and fail closed."""

import pytest

from packages.domain.operation_errors import (
    OPERATION_ERROR_CATEGORIES,
    source_collection_error_category,
    validate_operation_error_category,
)


EXPECTED_CATEGORIES = {
    "NETWORK_ERROR",
    "RATE_LIMITED",
    "SOURCE_CHANGED",
    "SOURCE_BLOCKED",
    "AUTH_REQUIRED",
    "CAPTCHA_REQUIRED",
    "ATS_UNSUPPORTED",
    "FORM_CHANGED",
    "FIELD_UNKNOWN",
    "UPLOAD_FAILED",
    "VALIDATION_FAILED",
    "JOB_CLOSED",
    "SUBMISSION_UNCONFIRMED",
    "AI_ERROR",
    "DOCUMENT_ERROR",
}


def test_catalogue_is_exactly_the_fifteen_issue_codes():
    assert OPERATION_ERROR_CATEGORIES == frozenset(EXPECTED_CATEGORIES)


@pytest.mark.parametrize("category", sorted(EXPECTED_CATEGORIES))
def test_validator_returns_each_canonical_category(category):
    assert validate_operation_error_category(category) == category


@pytest.mark.parametrize("value", [None, "", "SOURCE_TIMEOUT", "network_error", 7])
def test_validator_rejects_unknown_values_without_echoing_them(value):
    with pytest.raises(ValueError) as failure:
        validate_operation_error_category(value)
    assert str(failure.value) == "OPERATION_ERROR_CATEGORY_INVALID"
    assert repr(value) not in str(failure.value)


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("SOURCE_NETWORK_ERROR", "NETWORK_ERROR"),
        ("SOURCE_TIMEOUT", "NETWORK_ERROR"),
        ("SOURCE_TIME_LIMIT", "NETWORK_ERROR"),
        ("SOURCE_SERVER_ERROR", "NETWORK_ERROR"),
        ("SOURCE_HTTP_500", "NETWORK_ERROR"),
        ("SOURCE_RATE_LIMITED", "RATE_LIMITED"),
        ("SOURCE_HTTP_429", "RATE_LIMITED"),
        ("SOURCE_HTTP_401", "AUTH_REQUIRED"),
        ("SOURCE_CHANGED", "SOURCE_CHANGED"),
        ("SOURCE_REDIRECT_BLOCKED", "SOURCE_BLOCKED"),
        ("SOURCE_HTTP_403", "SOURCE_BLOCKED"),
    ],
)
def test_source_collection_mapping_returns_only_canonical_categories(provider_code, expected):
    assert source_collection_error_category(provider_code) == expected


@pytest.mark.parametrize("provider_code", [None, "", 500, ["SOURCE_HTTP_503"], "SOURCE_PAYLOAD_INVALID",
                                             "SOURCE_HTTP_404", "SOURCE_HTTP_5000", "SOURCE_HTTP_5XX",
                                             "token=PRIVATE"])
def test_source_collection_mapping_rejects_unclassified_provider_codes(provider_code):
    assert source_collection_error_category(provider_code) is None
