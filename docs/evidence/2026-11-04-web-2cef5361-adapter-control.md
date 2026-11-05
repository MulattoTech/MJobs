# MJ-REQ-224 — adapter control-exception propagation

- Run: `web-20261104T061539Z-2cef5361-8770-462c-be6c-c68302db77e8`
- Baseline: MJobs main `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf`
- Atomic objective owner: `refs/heads/webpro-claims/MJ-REQ-224` at `479768367df42295b81328453543aebe69aec622`
- Scope: Greenhouse form-validity evaluation and Recruitee challenge-count probe; local synthetic fixtures only.

## Reproduced failure before source change

Two new regression methods exercised three direct worker-control exceptions per adapter:
`OperationCancelled`, `OperationTimedOut`, `OperationControlUnavailable`.
Each exception was thrown directly by a controlled fake DOM evaluation/count operation while a
non-throwing cancellation callback was present. Both adapters swallowed the exception into a
generic `UNKNOWN` result. Exact red run: **6 failed subtests, 2 parent tests reported
passed by pytest's unittest subtest integration** (0.67 s).

## Correction

The two explicit DOM exception handlers re-raise those three worker-control exceptions unchanged,
including object identity. Ordinary browser exceptions are still converted to the existing fixed,
value-free `FORM_VALIDATION_UNAVAILABLE` and `CHALLENGE_CHECK_UNAVAILABLE` reasons.
No public form automation, capability flags, route normalization, external submission, data storage,
authentication, or control-budget policy changed.

## Local verification after correction

- Targeted new regression methods: **2 passed, 6 subtests passed** (0.60 s).
- All 11 built-in adapter suites plus the registry-wide controlled contract suite:
  **165 passed, 276 subtests passed**, zero failures (4.37 s).
- `python -m tools.verification.catalogue`: PASS.
- `python -m tools.verification.progress validate`: PASS.
- `python -m tools.verification.progress_dashboard --check`: CURRENT/PASS.
- `git diff --check`: PASS.
- Full suite, real browser/provider interaction, protected hosted CI, independent review,
  merge-candidate tests and production validation: **NOT RUN** at this local checkpoint.

## Acceptance and safety

This is a verified scoped failure-path correction, not full seven-stage adapter acceptance.
`MJ-REQ-224` remains PARTIAL and issue #225 remains OPEN. No real ATS requests,
personal documents, uploads, external job applications, paid model calls, production writes or
emergency-cutoff changes occurred.
