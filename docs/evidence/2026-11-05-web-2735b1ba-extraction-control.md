# MJ-REQ-004: resume extraction control failures

Run: `web-20261105T000428Z-2735b1ba4d46`
Claim: `refs/heads/webpro-claims/MJ-REQ-004`
Owner: `74835deafacc0e2e90de3db0a428c8b02198150a` (201 Created and exact readback verified)
Baseline: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`
Tested source: `6612f8c7b0bf1dd32980ab6f2176250626ecad3e`
Recorded source commit time: `2026-11-05T00:14:43Z`

## Reproduced defect

The parser's optional error recovery caught a worker deadline because
`OperationTimedOut` derives from `OSError`. Optional PDF OCR caught worker
cancellation and control-read failures as ordinary OCR failures. A failed check
could therefore be discarded when a later durable check succeeded.

Before changing production source, the new unit suite produced **8 FAIL, 6 PASS**.
Failures covered all three worker-control exceptions during OCR, worker timeouts
during native PDF and DOCX parsing, and generic failing callback types.
The new durable integration suite produced **4 FAIL**: all four operations
incorrectly persisted `COMPLETED` instead of the expected cancellation, timeout
or unavailable-control state. These original failures are not relabeled as PASS.

The integration fixtures inject a *single* failure through the real worker
control callback after an owned child starts. Subsequent checks are healthy,
which demonstrates why a late recheck alone does not repair this boundary.
They use synthetic uploads, isolated SQLite, actual worker execution, a reopened
Store and the real profile-scoped API readback.

## Implemented behavior

A local extraction control guard latches the original callback exception and
uses the existing parser/OCR stop protocol. Existing cleanup kills and reaps the
owned child and releases the OCR slot. The public extraction boundary then
rethrows the original exception object.

The extraction module does not import worker exception classes. Ordinary parser
and OCR failures retain their existing sanitized fallback behavior; native text
remains reviewable when an OCR tool itself fails. No schema, dependency, worker
policy, external provider, real applicant data or submission behavior changed.

## Verification

All commands run from the isolated feature worktree using its source and the
existing Python 3.12 environment read-only. No environment package changes,
heavy slot, shared database, real OCR provider, paid model or autonomous agent
was used.

| Command | Result |
| --- | --- |
| `python -m pytest -q -p no:cacheprovider tests/unit/test_resume_extraction_control_failures.py --tb=short --junitxml=outputs/web-2735b1ba-004/baseline-unit.xml` on unchanged baseline source | **8 FAIL, 6 PASS**, 0.72 s |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_resume_extraction_control_failures.py --tb=short --junitxml=outputs/web-2735b1ba-004/baseline-integration.xml` on unchanged baseline source | **4 FAIL**, 61.20 s |
| `python -m pytest -q -p no:cacheprovider tests/unit/test_resume_extraction_control_failures.py tests/unit/test_resume_extraction.py tests/unit/test_resume_ocr.py tests/integration/test_resume_extraction_control_failures.py --tb=short --junitxml=outputs/web-2735b1ba-004/focused-fixed.xml` | **63 PASS, 7 subtests PASS**, 48.23 s; zero failures/skips |
| `python -m tools.verification.catalogue` | PASS |
| `python -m tools.verification.progress validate` | PASS |
| `python -m tools.verification.progress_dashboard --check` | CURRENT |
| `git diff --check` and staged diff check | PASS |
| `python -m tools.verification.commit_provenance --commit HEAD --strict` on source commit | PASS; 3 authored Python files |

The successful working source was committed without changes to the executed
code or tests. The four durable tests verify exact terminal error classification,
no published extraction result, unchanged profile and uploaded source, terminal
intent replay, and successful recovery through a new explicit request. Unit
cases prove exception identity, owned-child reaping, OCR-slot release, normal
boolean cancellation, and privacy-safe ordinary OCR fallback.

Native Windows ran the tests. OCR process execution is replaced with an owned
sleeping Python child to exercise the real cancellation and cleanup wrappers;
this is not a real Poppler/Tesseract accuracy or Linux resource-limit acceptance
claim. New tests are included in normal hosted unit/integration discovery.

## Review and delivery limits

Author source/diff review completed; **independent review NOT RUN** in this chat.
Required hosted CI, actual merge-candidate verification, protected integration,
deployment and live browser checks are **PENDING / NOT RUN** at this source
checkpoint. Final facts belong in the linked PR and its later receipts.

**MJ-REQ-004 remains PARTIAL.** No canonical acceptance status, denominator,
GitHub Project completion state or Fleet acceptance total is promoted by this
slice. Full database acquisition/finalization, filesystem and browser operation
bounds remain outside this change.

Rollback is a protected revert of the source commit. No migration or stored-data
rewrite is needed. Rolling back restores the reproduced control-classification
defect, so preserve the regression evidence and assess the failed operation
rather than weakening its checks.
