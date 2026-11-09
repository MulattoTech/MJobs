"""Public, versioned disclosure of the bounded job-scoring projection.

The contract marker is captured with a new plan, not inferred when history is
read. Keep this version's meaning stable if a future evaluator changes its
payload. A recorded projection is neither a provider-retention promise nor a
record of the user approving some other AI operation.
"""

JOB_SCORING_CONTRACT = "JOB_SCORING_V1"

_RUNTIME_PROVIDERS = frozenset({
    "openai", "ollama", "openai-compatible", "local", "azure-openai", "azure",
})


def public_job_scoring_data_flow(binding: object) -> dict:
    """Return closed metadata only; never reflect stored text or configuration."""
    unknown = {"status": "UNKNOWN"}
    if (not isinstance(binding, dict)
            or binding.get("data_flow_contract") != JOB_SCORING_CONTRACT):
        return unknown
    provider, source = binding.get("provider"), binding.get("source")
    if provider == "disabled" and source == "DISABLED":
        return {"status": "DISABLED", "contract": JOB_SCORING_CONTRACT}
    if not ((provider == "openai" and source == "SAVED_PROFILE")
            or (isinstance(provider, str) and provider in _RUNTIME_PROVIDERS
                and source == "RUNTIME_CONFIGURATION")):
        return unknown
    # Fresh containers prevent a response consumer from modifying later reports.
    # The limits describe services.ai.consumer._prompt for this contract version;
    # tests also inspect the real prompt and synthetic provider request.
    return {
        "status": "RECORDED",
        "contract": JOB_SCORING_CONTRACT,
        "prompt_fields": [
            "job_title", "company", "job_description", "deterministic_match_score",
        ],
        "text_limits": {
            "job_title": 512, "company": 512, "job_description": 8_000,
            "prompt": 12_000,
        },
        "local_records": ["evaluation_input_snapshot", "advisory_results"],
        "provider_retention": "UNKNOWN",
    }
