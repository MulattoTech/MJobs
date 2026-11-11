"""Evidence-gated numbered observations for application journey steps 36-53.

This module records assertions made by the connected Playwright producer only
after the paired service readback, exact source/run binding, runtime identity,
and application lineage have been checked. It never submits an application.
"""

from dataclasses import dataclass
import re


APPLICATION_CHECKS = {
    36: frozenset({"application_prepared_readback", "profile_job_pins_match"}),
    37: frozenset({"selected_route_matches_application", "route_bound_to_job"}),
    38: frozenset({"supported_adapter_matches_provider", "provider_matches_route"}),
    39: frozenset({"playwright_form_opened", "selected_application_loaded"}),
    40: frozenset({"contact_fields_match_verified_profile", "contact_fields_readback_verified"}),
    41: frozenset({"resume_upload_matches_document", "resume_sha256_matches"}),
    42: frozenset({"cover_letter_upload_matches_document", "cover_letter_sha256_matches"}),
    43: frozenset({"approved_answers_match_questions", "approved_answers_readback_verified"}),
    44: frozenset({"uncertain_question_paused", "submit_not_attempted"}),
    45: frozenset({"explicit_answer_approval_persisted", "approval_bound_to_question"}),
    46: frozenset({"reviewed_application_resumed", "stale_approval_rejected"}),
    47: frozenset({"current_form_validated", "profile_document_pins_match"}),
    48: frozenset({"dry_run_receipt_persisted", "no_external_post_recorded"}),
    49: frozenset({"owned_mock_received_submit", "external_submission_absent"}),
    50: frozenset({"authoritative_confirmation_detected", "confirmation_count_one"}),
    51: frozenset({"history_receipt_persisted", "repeated_readback_idempotent"}),
    52: frozenset({"tracker_matches_application", "tracker_survives_reload"}),
    53: frozenset({"audit_sequence_complete", "audit_scope_matches_application",
                   "audit_values_redacted"}),
}

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_RESULT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:;()/_-]{0,239}\Z")
_STAGE_KINDS = frozenset({"PREPARE", "APPLY_MOCK"})


@dataclass(frozen=True)
class AssertionEvidence:
    """One producer-side UI assertion plus its matching service readback.

    Callers should construct this immediately after the actual browser assertion
    and corresponding API readback; a bare boolean is rejected. These paired
    values are producer claims, not independent attestations or acceptance review.
    """

    ui: bool
    readback: bool


def _valid_runtime_identity(runtime_identity):
    """Require the runner-supplied binding shape; the coordinator authenticates it."""
    if not isinstance(runtime_identity, dict):
        return False
    manifest = runtime_identity.get("source_manifest_sha256")
    images = runtime_identity.get("images")
    if not isinstance(manifest, str) or not manifest:
        return False
    if not isinstance(images, dict) or not images or not isinstance(images.get("api"), str):
        return False
    return all(isinstance(name, str) and name and isinstance(image, str) and image
               for name, image in images.items())


def _validate_lineage(step, lineage):
    if not isinstance(lineage, dict) or set(lineage) != {
            "step", "operation_id", "request_id", "operation_kind",
            "application_id", "profile_id", "job_id"}:
        return False
    if lineage.get("step") != step or type(lineage.get("step")) is not int:
        return False
    kind = lineage.get("operation_kind")
    if kind not in _STAGE_KINDS:
        return False
    if step in {36, 37} and kind != "PREPARE":
        return False
    if step >= 38 and kind != "APPLY_MOCK":
        return False
    return all(isinstance(lineage.get(key), str) and _SAFE_ID.fullmatch(lineage[key])
               for key in ("request_id", "application_id", "profile_id", "job_id")) and (
                   isinstance(lineage.get("operation_id"), str)
                   and re.fullmatch(r"[a-f0-9]{64}", lineage["operation_id"]) is not None)


def record_application_step(journal, step, *, observable_result, assertions,
                            runtime_identity, stage_lineage=None, document_id=None):
    """Record one application step from paired UI/readback evidence.

    The helper determines PASS itself. Missing or mismatched runtime, identity,
    operation lineage, or document proof downgrades the row to PARTIAL. The
    paired assertion values are not independent proof; the real callsite must
    run Playwright assertions and scoped API readbacks, and the coordinator's
    runtime preflight must bind the supplied IDs and hashes. It
    carries step-local operation/request IDs in the UI artifact's stage_lineage
    list, keeping them separate from the final submit IDs in recovery identity.
    """
    if step not in APPLICATION_CHECKS or type(step) is not int:
        raise ValueError("unsupported application acceptance step")
    if (not isinstance(observable_result, str) or not _SAFE_RESULT.fullmatch(observable_result)
            or observable_result != observable_result.strip()
            or "://" in observable_result or "@" in observable_result):
        raise ValueError("observable_result must be bounded sanitized text")

    names = APPLICATION_CHECKS[step]
    if not isinstance(assertions, dict) or set(assertions) != names:
        raise ValueError("assertions must exactly match the registered application checks")
    if any(not isinstance(assertions[name], AssertionEvidence) or
           type(assertions[name].ui) is not bool or type(assertions[name].readback) is not bool
           for name in names):
        raise ValueError("each check requires paired Playwright and service-readback evidence")

    binding = getattr(journal, "binding", {})
    source = binding.get("source", {}) if isinstance(binding, dict) else {}
    source_commit = source.get("commit") if isinstance(source, dict) else None
    run_id = getattr(journal, "acceptance_run_id", None)
    source_ok = isinstance(source_commit, str) and bool(source_commit)
    run_ok = isinstance(run_id, str) and bool(run_id)
    # The shared observation schema requires both bindings. Without either,
    # leave the step unobserved instead of manufacturing an unanchored PARTIAL.
    if not (source_ok and run_ok):
        return None
    runtime_ok = _valid_runtime_identity(runtime_identity)

    profile_id = getattr(journal, "selected_profile_id", None)
    application_id = getattr(journal, "selected_application_id", None)
    job_id = getattr(journal, "selected_application_job_id", None)
    selected_ids_ok = all(isinstance(value, str) and _SAFE_ID.fullmatch(value)
                          for value in (profile_id, application_id, job_id))
    lineage_ok = (_validate_lineage(step, stage_lineage) and selected_ids_ok
                  and stage_lineage["profile_id"] == profile_id
                  and stage_lineage["application_id"] == application_id
                  and stage_lineage["job_id"] == job_id)

    documents = set(getattr(journal, "application_document_ids", []))
    document_ok = True
    if step in {41, 42}:
        document_ok = (isinstance(document_id, str) and _SAFE_ID.fullmatch(document_id) is not None
                       and document_id in documents)
    elif document_id is not None:
        document_ok = (isinstance(document_id, str) and _SAFE_ID.fullmatch(document_id) is not None
                       and document_id in documents)

    structural_proof_ok = (source_ok and run_ok and runtime_ok and selected_ids_ok
                           and lineage_ok and document_ok)
    checks = [{"name": name,
               "passed": structural_proof_ok and assertions[name].ui and assertions[name].readback}
              for name in sorted(names)]
    status = "PASS" if checks and all(item["passed"] for item in checks) else "PARTIAL"

    continuity = {}
    for key, value in (("profile_id", profile_id), ("application_id", application_id),
                       ("job_id", job_id)):
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            continuity[key] = value
    if document_ok and document_id is not None:
        continuity["document_id"] = document_id

    if lineage_ok:
        record_lineage = getattr(journal, "record_stage_lineage", None)
        if not callable(record_lineage):
            raise RuntimeError("journal does not support stage lineage")
        record_lineage(dict(stage_lineage))

    return journal.record_observation(
        step=step,
        status=status,
        observable_result=observable_result,
        evidence_kind="CONTROLLED_FIXTURE",
        checks=checks,
        continuity=continuity,
    )
