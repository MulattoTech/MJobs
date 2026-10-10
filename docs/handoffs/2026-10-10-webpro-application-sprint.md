# MJobs application reliability handoff ? 2026-10-10

## Verified and published source
Baseline main: `fa3e29aef27f503bef23c74ea18f4a2fc32c03b7`. Source main/branch read back at `25fea60889ad13cb5e837ab2263324f84fd20a04`, tree `d2fcf4387ff9dfca71fa779eb37001e4caae924c`.
Branch: `chatgpt/durable-application-attempts-20261010`. Worktree: `C:/Tools/Dev/MJobs-WebSprint-20261010`.
Three product commits:
- `75f285f95fae7039fa5c83b9cd11eba4ad6cb0f6`: immutable attempt receipts, serialized admission, scoped API and connected history.
- `96beac44b2115230c04b967ac113a5939855f19b`: explicit receipt-only recovery; lost-confirmation/restart test confirms exactly one mock submission before and after reconciliation/replay.
- `25fea60889ad13cb5e837ab2263324f84fd20a04`: frozen hash-verified document buffers, Chromium selected-byte verification, late-tamper rejection, supporting selection in replay identity, and visible digest manifests.

The final metadata commit may be a descendant of this tested source. Verify current main rather than assuming this document embeds its own future SHA. GitHub issue receipts identify the final published tip.

## Exact combined acceptance
Python: 86 run, 84 passed, 2 failed, 0 skipped; all 41 newly added tests passed.
Frontend: 99 files, 567 passed, 0 failed/skipped. TypeScript/Vite build passed; existing 783.24 kB bundle warning remains.
The two Python failures were reproduced on untouched baseline `fa3e29aef27f503bef23c74ea18f4a2fc32c03b7`:
- `test_conditional_required_dropdown_uses_known_preflight_mapping`
- `test_sensitive_required_question_persists_review_and_stops_before_browser`
Do not call the combined suite PASS. Earlier introduced debug-capture regressions were fixed by preserving the existing 32-hex attempt-ID contract, and pass the final run.

Evidence:
- `docs/evidence/2026-10-10-durable-application-attempts.json` ? integrated batch commands, times, hashes, baseline failures and input preservation.
- `docs/evidence/2026-10-10-confirmation-reconciliation.json` ? exactly-once local submission followed by receipt-only recovery.
- `docs/evidence/2026-10-10-verified-upload-bytes.json` ? file tampering/immutable buffers/browser readback.
Git blob SHA-256 manifests are independently checkable with `git show <source_commit>:<path>` and SHA-256. Worktree hashes may differ after Windows line-ending conversion; compare Git blob hashes for cross-checkout verification.

## Honest requirement progress
Original MJ-REQ (366): PASS 102 ? 102; PARTIAL 99 ? 101; NOT_RUN 165 ? 163.
Supplemental MJ-REF (59): unchanged PASS 19, PARTIAL 4, NOT_RUN 36.
MJ-REQ-138 and MJ-REQ-144 move NOT_RUN ? PARTIAL. MJ-REQ-134 and MJ-REQ-145 gain evidence while remaining PARTIAL. No original/supplemental denominator mixing, no issue closed merely for merge.

## Constraints and remaining work
Real ATS submission and hosted CI/review/AI/cloud remain OFF. No production runtime, active coding agent, host security/network setting, driver or firmware was restarted/changed. The running personal MJobs application was not redeployed; connected acceptance used an isolated built application with synthetic data. PostgreSQL and full-repository Python discovery were NOT_RUN.
Only local MOCK execution is instrumented. Automatic takeover of abandoned RUNNING attempts is intentionally absent; expired/legacy reservations are not proof a process stopped. Stable explicit request replay is implemented at the adapter boundary; the current worker does not yet pass a stable caller request ID. File verification proves browser selection before review, not external provider acceptance.

## Resume without collateral changes
Recheck live issue/path claims and local/remote ancestry first. Preserve every foreign worktree/claim. Existing form-analysis owners hold paths involved in the baseline failures.
The most direct next leaf is worker-operation request identity and cancellation/retry recovery with real restart tests. Further provider work should first complete shared adapter contracts rather than bypass MJ-PR-034 dependencies. Full original requirements remain OPEN/PARTIAL.

## Coordination closeout
This session's claim keys: `codex:webpro-attempts-20261010`, `codex:webpro-confirmation-20261010`, `codex:webpro-upload-integrity-20261010`.
Its lock was acquired at `2026-10-10T07:59:36.794894Z` by lane codex. Release only these claims and this exact lock after the final metadata push/readback. Live bus and final GitHub issue receipts are authoritative for completed release; never steal a different owner's lock.
