# MJ-REQ-224 — preserve control failures in remaining DOM probes

Run: `web-20261105T000010Z-9fafaf63-079d-4e7c-9b97-d858264fd5b2`.
Baseline: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
Atomic claim: `refs/heads/webpro-claims/MJ-REQ-224`, owner
`4337a41c754b4609fad7a5ab89dcb5baf185f883` (HTTP 201 and matching readback).

## Observed defect and correction

Eight broad exception handlers in the shared form analyzer and Breezy,
SmartRecruiters, Teamtailor and Workable DOM probes converted direct
`OperationCancelled`, `OperationTimedOut` and `OperationControlUnavailable`
failures into form metadata or ordinary browser-error fallbacks. A second
callback check could also erase a control failure raised by the first check.
These failures now propagate with their original type and object identity.
Ordinary errors retain the existing value-free responses; Breezy's optional
visibility probe still preserves its prior metadata without visibility proof.
Greenhouse/Recruitee files in PR #928 are not changed by this follow-up.

## Reproducible verification

- Unchanged baseline with the new test module: **63 FAIL, 20 PASS**, 0.90 s.
  All failures are missing propagation or rechecking away a control failure.
- Corrected focused module: **83 PASS**, 0.54 s, zero failures/skips.
- Expanded adapter, shared analyzer, form-field privacy, provider dispatch and
  durable form-review worker tests: **292 PASS, 301 subtests PASS**, 127.15 s,
  zero failures/errors/skips. This uses synthetic data and read-only fake DOMs.
- Catalogue validation: PASS. Progress journal validation: PASS.
  Progress dashboard: CURRENT. Diff whitespace check: PASS.

Focused command: `python -m pytest -q -p no:cacheprovider tests/unit/test_adapter_operation_control.py --tb=short`.
Expanded command: the same pytest options, adding
`tests/unit/test_application_adapter_contract.py`,
`tests/unit/test_application_form_analysis.py`,
`tests/unit/test_application_form_field_security_227.py`,
`tests/unit/test_application_form_review.py`,
`tests/unit/test_application_form_review_providers.py`,
`tests/unit/test_application_form_review_worker.py`, and the eleven
`tests/unit/test_*_application_adapter.py` suites (explicit expanded paths).

The containing PR identifies the published commit and current hosted checks.
Local results bind the unchanged product/test blobs in that commit; evidence
text is added after execution. Earlier red failures are retained here.

## Acceptance and release boundaries

MJ-REQ-224 remains **PARTIAL**; issue #225 stays open. These tests do not prove
all seven adapter journey dimensions. No capability flags, navigation, form
filling, uploads, submissions, persistent schema, live browser profiles, user
data, or Fleet settings change. No paid model or real ATS request was made.
Protected current-candidate CI, independent review, serialized integration and
production deployment remain separate gates. Roll back this isolated commit
through the protected workflow if necessary; no data migration is required.

## Shared-history scanner correction

The original required scan (run 41939845186, redacted artifact 13376340947) failed on exactly two historical findings in PR937's evidence note, outside this change's authored source. Both values were independently verified against immutable source Git objects; review: https://github.com/MulattoTech/MJobs/pull/937#pullrequestreview-5777892865.

Under the separately acquired SECRET-SCAN-FINGERPRINTS claim, this follow-up reuses the byte-identical four-line correction already reviewed in PR937: two explanatory comments and two exact commit/path/rule/line fingerprints. No scanner rule, workflow, directory or generic pattern is exempted. The original failure remains recorded. A fresh passing protected scan is still required; focused scanner-policy tests do not substitute for that scan.

The adapter implementation and its executed results above are unchanged by this correction. Independent review/execution of the adapter source is recorded at https://github.com/MulattoTech/MJobs/pull/940#issuecomment-6646020493 (142 tests and 171 subtests passed).
