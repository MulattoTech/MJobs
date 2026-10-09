"""Conservative advisory recruiter classification from explicit evidence only."""

import pytest

from services.discovery.recruiter_classification import classify_recruiter


def _evidence(classification, *, subject_type="COMPANY", subject_id="company-1",
              source="VERIFIED_COMPANY_CONTEXT", evidence_ref="context:12",
              current=True, explicit=True, confidence=0.9):
    return {
        "classification": classification,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "source": source,
        "evidence_ref": evidence_ref,
        "current": current,
        "explicit": explicit,
        "confidence": confidence,
    }


@pytest.mark.parametrize("classification", [
    "DIRECT_EMPLOYER", "STAFFING_AGENCY", "RECRUITER",
])
def test_explicit_current_attributable_claims_return_advisory_classifications(classification):
    result = classify_recruiter(
        [_evidence(classification)], expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == classification
    assert result["confidence"] == 0.9
    assert result["evidence"] == [_evidence(classification)]
    assert result["unknowns"] == []
    assert result["reasons"]
    assert "reject" not in result
    assert "apply" not in result


def test_job_evidence_must_match_the_requested_job_identity():
    result = classify_recruiter(
        [_evidence("RECRUITER", subject_type="JOB", subject_id="another-job")],
        expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert result["evidence"] == []
    assert "EVIDENCE_SUBJECT_MISMATCH" in result["unknowns"]


@pytest.mark.parametrize(("evidence", "expected_code"), [
    (None, "NO_CURRENT_ATTRIBUTABLE_EVIDENCE"),
    ([], "NO_CURRENT_ATTRIBUTABLE_EVIDENCE"),
    ("unattributed recruiter claim", "EVIDENCE_INVALID"),
])
def test_missing_or_unstructured_evidence_remains_unknown(evidence, expected_code):
    result = classify_recruiter(
        evidence, expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert result["evidence"] == []
    assert expected_code in result["unknowns"]


def test_stale_or_nonexplicit_claims_do_not_classify_current_work():
    evidence = [
        _evidence("STAFFING_AGENCY", current=False),
        _evidence("RECRUITER", source="UNVERIFIED_TEXT", explicit=False),
    ]

    result = classify_recruiter(
        evidence, expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert result["evidence"] == []
    assert set(result["unknowns"]) == {"STALE_OR_NONEXPLICIT_EVIDENCE_IGNORED"}


def test_conflicting_current_claims_produce_unknown_and_preserve_attribution():
    evidence = [
        _evidence("DIRECT_EMPLOYER", subject_type="COMPANY", evidence_ref="company:verified-domain"),
        _evidence("RECRUITER", subject_type="JOB", subject_id="job-1",
                  source="SOURCE_PROVIDED_JOB_METADATA", evidence_ref="posting:byline", confidence=0.85),
    ]

    result = classify_recruiter(
        evidence, expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert len(result["evidence"]) == 2
    assert result["evidence"] == sorted(
        result["evidence"], key=lambda item: (
            item["subject_type"], item["subject_id"], item["classification"],
            item["source"], item["evidence_ref"], item["confidence"],
        ),
    )
    assert "CONFLICTING_CURRENT_CLAIMS" in result["unknowns"]


@pytest.mark.parametrize("override", [
    {"source": ""},
    {"evidence_ref": ""},
    {"confidence": True},
    {"confidence": 1.01},
    {"classification": "LIKELY_AGENCY"},
    {"classification": []},
    {"subject_type": []},
    {"current": "yes"},
    {"extra": "not part of the classifier contract"},
])
def test_malformed_or_unsupported_claims_fail_closed(override):
    claim = _evidence("STAFFING_AGENCY")
    claim.update(override)

    result = classify_recruiter(
        [claim], expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert result["evidence"] == []
    assert "INVALID_OR_UNATTRIBUTABLE_EVIDENCE" in result["unknowns"]


def test_staffing_classification_never_creates_rejection_or_eligibility_policy():
    result = classify_recruiter(
        [_evidence("STAFFING_AGENCY")],
        expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "STAFFING_AGENCY"
    assert not any(key in result for key in (
        "reject", "rejected", "eligible", "eligibility", "apply", "application_policy",
    ))


def test_explicit_unknown_claim_stays_unknown_and_keeps_its_provenance():
    claim = _evidence("UNKNOWN")

    result = classify_recruiter(
        [claim], expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert result["confidence"] is None
    assert result["evidence"] == [claim]
    assert "EXPLICIT_EVIDENCE_INCONCLUSIVE" in result["unknowns"]


def test_company_claim_requires_the_requested_company_identity():
    result = classify_recruiter(
        [_evidence("DIRECT_EMPLOYER", subject_type="COMPANY", subject_id="other-company")],
        expected_job_id="job-1", expected_company_id="company-1",
    )

    assert result["classification"] == "UNKNOWN"
    assert "EVIDENCE_SUBJECT_MISMATCH" in result["unknowns"]
