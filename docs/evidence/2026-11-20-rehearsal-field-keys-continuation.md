# Rehearsal field-key continuation on current main

Run: `mjobs-web-20261120-d9b89794`.
Scope: [PR #1034](https://github.com/MulattoTech/MJobs/pull/1034),
[MJ-REQ-136 / issue #137](https://github.com/MulattoTech/MJobs/issues/137).

## Change and integration

The existing PR fixes rehearsal previews that rejected valid form keys such as
`candidate[first_name]`, `field[]`, and `_custom`. Its field-specific guard
matches the backend's full-string ASCII grammar and 100-character limit, while
the separate 128-character entity/request/receipt ID rules remain unchanged.

No replacement product implementation was authored in this continuation.
Original source, regression tests, evidence, and commit attribution are retained.
The primary and original PR worktrees were preserved.

- Previous PR head: `6fbfd95bad6c6b53e2a73b1b6c1f6d2b5cfd3476`.
- Main incorporated: `db9cdc54f9b0fe060ffb58ad5afd9fa24ec59216`.
- Tested candidate tree: `f028c28f7f90258c4039035e1d44b884987b3fc7`.
- Merge commit preserving that exact tree: `2d3c097255f8a35e96eba40ef526435241b60b95`.
- Continuation branch: `web/20261120-d9b89794/rehearsal-fields`.
- Existing publication branch: `web/ef1809b6/rehearsal-field-keys`.

Tests ran on the staged current-main candidate before committing. Root verified
that the committed tree is exactly identical to that tested tree. Later
checkpoint/evidence changes preserve the tested source and test blobs.

## Fresh executed verification

| Command / check | Result |
| --- | --- |
| `npm --prefix apps/web test -- --coverage=false src/application-rehearsal.test.tsx` | 39 passed, 0.200 seconds |
| `npm --prefix apps/web run build` (includes TypeScript) | PASS |
| ESLint on both affected TSX files | PASS |
| `pytest -q -p no:cacheprovider tests/unit/test_application_rehearsal.py tests/browser/test_application_rehearsal.py --tb=short` | 11 passed, no skips, 23.06 seconds |
| Catalogue, progress journal, progress dashboard freshness | PASS |
| Ownership-index freshness, public repository policy | PASS |
| Staged/unstaged whitespace and merge provenance | PASS |

Python verification used pytest.main with a small source-identity plugin,
asserting that API/rehearsal imports resolve to the isolated candidate.
The browser used the newly built frontend, installed Chromium 1243, synthetic
SQLite records, and ephemeral local fixtures. Opening and reloading the preview
preserve blockers; acknowledgement creates one local review receipt, with
application/attempt state unchanged and nonlocal browser traffic blocked.

Environment: cached Python 3.12; local Node 27 alpha / npm 12.2.3.
Private frontend dependencies were copied after confirming matching lockfiles.
No shared installation was changed. Hosted Node 24 verification remains a
separate exact-head gate. The existing Vite bundle-size advisory is nonfatal.

## Independent review

A separate AI reviewer found no blocking findings. It checked the exact ASCII
field grammar, maximum length, invalid leading characters/control/newline input,
unchanged receipt IDs, retained `submission_enabled === false` check, safe text
rendering, local receipt behavior, and browser negative controls.
The reviewer performed source review; runtime results above belong to the test
agent. This is not a human GitHub approval.

The three original affected Git blobs remain unchanged:

- `application-rehearsal.tsx`: `2d81d16d33b1717d5935bdba2bcaca186a3f7294`
- `application-rehearsal.test.tsx`: `b12d230f1ed77f11fe5cbe83b30c3e9c87b39e71`
- `test_application_rehearsal.py`: `ef74f29c681c6cc57c2e59cf4eab714ac18abbba`

## Gates and limits

This is one bounded nonforce update to existing PR1034, with no new PR or source
duplication. Fresh publication preflight and hosted checks are required on the
containing published commit. Protected merge requires the explicit user approval
reserved by the engineering brief.

MJ-REQ-136 remains PARTIAL. Canonical totals are unchanged: originals
188 PASS / 82 PARTIAL / 96 NOT_RUN of 366; references
19 PASS / 4 PARTIAL / 36 NOT_RUN of 59. No full acceptance is claimed.

PostgreSQL-specific checks, authenticated public functionality, production
deployment, and real employer submission were not performed. Existing runtime
and fixture stacks were preserved. This patch has no migration or user-data
rewrite; later rollback is an ordinary protected source revert.

## Approved serial integration continuation

The user explicitly approved serial protected integration of PR1045 then PR1034, including the necessary base refresh and required checks. Production deployment remains separately reserved.

PR1045 merged at 2026-11-20T06:53:35Z as `d9e3bf11aa135a58bc1aa786edddae8cec478f66`; its tree exactly matches the previously verified `abe15feb` candidate. PR1034 refreshed cleanly onto that main.

The combined tree `4437b70a9b9c577e327d1de44211152172f25fa2` was committed identically as `94b1502b5052869411bdacbe1c52da9df91e4998`. Fresh execution on this composition passed **63 frontend tests** across five suites and **41 Python/API/browser tests plus 28 subtests**, with zero failures/skips (pytest 45.12 seconds). TypeScript and a fresh production build passed; the existing bundle-size advisory remains. Logs/JUnit are retained as `.tmp/approved-integration-*`.

The Python run combined the six comparison files listed in the comparison evidence with `tests/unit/test_application_rehearsal.py` and `tests/browser/test_application_rehearsal.py`. The frontend run combined the three comparison suites, application rehearsal, and main characterization. This verifies the integrated feature composition; the final documentation/checkpoint commit leaves that tested source unchanged.

Fresh hosted checks on the updated published head remain required before the already authorized PR1034 merge. Earlier `ed78ee3d` checks passed all 15 records but do not substitute for the new head. MJ-REQ-136 remains PARTIAL and MJ-REF-039 remains NOT_RUN at this checkpoint; canonical acceptance is not changed by these integration results.

The separately completed synthetic guarded backup/upgrade/fresh-project restore is now recorded in [the recovery evidence](2026-11-20-guarded-recovery-d9b89794.json). Its executed candidate is explicitly `ed78ee3d`, and it is not production deployment evidence.
