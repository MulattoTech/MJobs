"""Canonical durable operation type vocabulary."""


REQUIRED_OPERATION_TYPES = frozenset({
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

# These persisted kinds predate the canonical requirement vocabulary. Keep them
# registered so saved operations remain readable and executable after upgrade.
RUNTIME_OPERATION_TYPES = frozenset({
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

OPERATION_TYPES = REQUIRED_OPERATION_TYPES | RUNTIME_OPERATION_TYPES


def validate_operation_type(value):
    """Return one registered type or a fixed, value-free failure code."""
    if type(value) is not str or value not in OPERATION_TYPES:
        raise ValueError("OPERATION_TYPE_INVALID")
    return value


__all__ = [
    "OPERATION_TYPES",
    "REQUIRED_OPERATION_TYPES",
    "RUNTIME_OPERATION_TYPES",
    "validate_operation_type",
]
