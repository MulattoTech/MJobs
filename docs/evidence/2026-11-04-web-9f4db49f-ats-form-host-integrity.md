# ATS form-action hostname integrity — 2026-11-04

- Run: `web-20261104T063306Z-9f4db49f-c843-4a8b-808f-20dc2b6ae570`
- Requirement: `MJ-REQ-063`; GitHub issue #64 (ATS auto-detection).
- Baseline: `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf` on `main`.
- Claim: `ATS-FORM-ACTION-HOST-INTEGRITY`, owner `a612d060ae3bccf079a0c235f64986e3eaf91aa2`.
- Scope: `services/ats_detection/detector.py` and `tests/unit/test_ats_detection.py`.

## Reproduction

The detector parsed form action hostnames but also searched an entire untrusted action URL for
`PUBLIC_API` signatures and promoted any textual match to high-confidence `FORM_ACTION`
evidence. A form on `attacker.example` with `?target=api.smartrecruiters.com` could therefore
be misclassified as SmartRecruiters. A genuine Lever script paired with the unrelated form
could become a false cross-provider `AMBIGUOUS` result.

The added focused tests were executed against unmodified implementation before the repair:
`python -m pytest -q -p no:cacheprovider tests/unit/test_ats_detection.py -k form_actions --tb=short`.
Result: **5 failing subtests**, including query, path, userinfo, relative action, and mixed-provider
spoofing; provider-hosted form actions remained valid.

## Repair and verification

Only the parsed action hostname may establish a strong form-action provider signal.
The duplicate raw-string `PUBLIC_API` promotion was removed. Existing weak
`URL_STRUCTURE` evidence is preserved. Legitimate vendor-hosted SmartRecruiters and Lever
forms still yield `MATCH` with `FORM_ACTION` evidence.

`python -m pytest -q -p no:cacheprovider tests/unit/test_ats_detection.py --tb=short`
after repair: **24 passed, 75 subtests passed**.

## Acceptance boundaries

This evidence supports a targeted false-positive regression fix, **not** complete ATS
auto-detection acceptance. `MJ-REQ-063` remains **PARTIAL**; no canonical acceptance
row or dashboard counter is promoted. No live ATS network calls, external AI model calls,
deployment, or production mutations were made for this repair. Further integration,
provider coverage, CI and protected review are separate gates.

Additional deterministic regression gate:
`python -m pytest -q -p no:cacheprovider tests/unit/test_ats_detection.py tests/unit/test_lever_source.py tests/integration/test_application_capability_filter.py --tb=short`
Result: **36 passed, 82 subtests passed** (7.13 seconds).
`python -m tools.verification.progress validate`: **PASS**.
`python -m tools.verification.progress_dashboard --check`: **CURRENT**.
`python -m tools.verification.catalogue`: **PASS**.
`git diff --check`: **PASS**.
