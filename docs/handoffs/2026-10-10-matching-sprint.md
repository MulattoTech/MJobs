# MJobs matching sprint — integration handoff

## Status and authority

Three locally verified product slices plus unit-test contract repairs are preserved on this branch. They are not integrated into main and do not establish release readiness. Only the legitimate integration-lock owner may update main or canonical planning.

- Repository: `MulattoTech/MJobs` (private).
- Worktree: `C:/Tools/Dev/MJobs-WebMatching-20261010`.
- Branch: `chatgpt/preference-weighting-20261010`.
- Baseline main: `f399e00da63c7bf14cd5281b71bba5daf8d62e10`; root remained clean and unchanged at final source validation.
- Final accepted code/test source: `29e8dc2233c16484799d298f4a6cfe4501272af5`.
- Source tree: `bad63297707d91c1e891cb3252fe65c753ec21ff`.
- This handoff's evidence-only commit follows that source. Verify its exact branch SHA through GitHub and completion receipts in issues #115, #118 and #292; no self-referential SHA is invented here.
- Integration: `BLOCKED_BY_ACTIVE_INTEGRATOR`; tracker: `TRACKER_REFRESH_PENDING`.
- Existing lock: codex, PID 24568, acquired 2026-10-10T14:22:31.735636Z. Re-read current lock state; do not steal or clear it.

## Product checkpoints

| Slice | Issue / requirement | Code commit | Verified behavior |
|---|---|---|---|
| Native preference weights | #118 / MJ-REQ-117 | `d2bccd8d7eaf01ec1e6c3986a7d99d79d513ee03` | Title, salary overlap, arrangement, employment type and remote region use deterministic personal evidence; UNKNOWN is neutral and source fit scores cannot override it. |
| No-write draft ranking preview | #118 / MJ-REQ-117 | `1cbfc585f7445f35d6109cba51676b95aae7f688` | Editor previews current/draft scores, sample ranks and contributions; bounded scope/version checks and stale-response invalidation; implicit Skills weight made visible. |
| Positive career evidence | #115 / MJ-REQ-114 | `97d5ea4dce1a9c0f49f585ff8516b3683079dd15` | Supported negative, aspirational or conflicting mentions do not inflate skill/experience coverage; invalid confidence excluded; persisted explanations survive reopening. |
| Unit-contract repairs | #292 / MJ-REQ-291 | `29e8dc2233c16484799d298f4a6cfe4501272af5` | Three pristine-baseline test defects repaired without changing credential/Compose/heatmap runtime code; added invalid-project/port denial checks. |

UI is connected through the existing scoring editor and router. Neither `apps/api/main.py` nor `apps/web/src/main.tsx` was edited. Other agents' worker/application/planning claims were preserved.

## Reproduction and evidence

Environment: Windows, Python 3.12.10, Node 25.9.0, Vitest 5.0.3, Vite 8.3.1, isolated SQLite and headless Chromium. The root virtual environment was used read-only; web dependencies were copied into this worktree instead of modified in another lane.

```powershell
Set-Location C:\Tools\Dev\MJobs-WebMatching-20261010
$env:PYTHONIOENCODING = 'utf-8'
& C:\Tools\Dev\MJobs\.venv\Scripts\python.exe -m pytest tests/unit tests/integration/test_positive_skill_evidence.py tests/integration/test_preference_weighting.py tests/integration/test_scoring_draft_preview.py tests/integration/test_explainable_matching.py tests/integration/test_matching_explanation_contributions.py tests/integration/test_matching_explanation_stale_context_recovery.py tests/integration/test_role_scoring_search_flow.py tests/integration/test_selected_search_recalculation.py tests/integration/test_selected_search_default_score_persistence.py tests/integration/test_scoring_profiles_api.py tests/integration/test_scoring_profile_router_mount.py tests/browser/test_preference_weighting.py tests/browser/test_scoring_draft_preview.py tests/browser/test_role_scoring_profile_reload_match.py -q --tb=short
Push-Location apps/web
npm.cmd run test -- --reporter=dot
npm.cmd run build
Pop-Location
python -m tools.coordination.lane_coordination validate
git diff --check
```

Final result: **1,054 tests plus 546 subtests passed, zero failures/errors/skips**. This covers all 125 unit test files and the explicitly listed API/persistence/browser cases, not every integration/browser test. JUnit has 1,600 cases including subtests. Frontend: **583 tests in 101 files passed**, zero failures/skips. TypeScript/Vite build passed with the existing chunk-size warning (791.54 kB JavaScript). Compilation, scoped secret-pattern, diff and claim checks passed.

Earlier counts overlap and must not be summed. Initial broad regression had 1,051 tests and 542 subtests passing plus 2 failures and 1 error. Those same failures reproduced in a pristine archive of `f399e00da63c7bf14cd5281b71bba5daf8d62e10`: 10 tests and 4 subtests passed, 2 failed, 1 error. They were repaired rather than ignored or suppressed.

Evidence:

- `docs/evidence/2026-10-10-preference-weighting.json`
- `docs/evidence/2026-10-10-scoring-draft-preview.json`
- `docs/evidence/2026-10-10-positive-skill-evidence.json`

The latest evidence contains exact commands, source/tree and working-byte/Git-blob SHA-256 hashes, all sprint product/test Git-blob hashes, local raw-log hashes and limitations. Raw logs, fixture databases, copied dependencies and built assets are not published. A zero-byte test-created `events.jsonl` was moved out after tests stopped.

## Canonical acceptance — unchanged

| Catalogue | Before | After |
|---|---|---|
| Original MJ-REQ | 102/366 PASS; 101 PARTIAL; 163 NOT_RUN | 102/366 PASS; 101 PARTIAL; 163 NOT_RUN |
| Supplemental MJ-REF | 19/59 PASS | 19/59 PASS |
| Release | UNKNOWN | UNKNOWN |

MJ-REQ-114 and MJ-REQ-117 remain PARTIAL. MJ-REQ-291 remains NOT_RUN: a broad unit run is not full Test Matrix acceptance. Publication or a slice PASS never automatically promotes an entire requirement.

## Integration procedure

1. Re-read AGENTS.md, coordination rules, live claims/lock, remote main and worktree status. Do not reset, clean, stash, rebase or overwrite anyone else's work.
2. Inspect the branch and hashes above. Preserve evidence-to-source relationships.
3. Only the legitimate integrator may integrate after current ancestry/path checks through the non-force policy. This handoff does not authorize lock theft.
4. Re-run exact validation on the integration candidate. Keep PARTIAL/NOT_RUN unless every canonical criterion has accepted evidence, then refresh the tracker under the proper lock.
5. Recheck hosted execution policy before publishing. Hosted CI/AI review, paid providers and real applications remain unauthorized.

Owned codex child claims: `webpro-preference-weighting-20261010`, `webpro-preference-factor-extraction-20261010`, `webpro-draft-preview-20261010`, `webpro-positive-evidence-20261010`, `webpro-unit-contract-repairs-20261010`. Check their issue closeout receipts. Release only these after final non-force publication/readback and handoff; never release the integration lock. Retain the branch and worktree for integration.

## Limits and next valuable slices

The preview inspects at most 100 current-profile stored matches; ranks are sample-only, not global or a submission shortlist. Hidden/blacklisted historical matches may appear, stated in the UI. Unsupported advanced workspace job filters reject explicitly. Preview scores are recomputed; saving and persisted recalculation remain separate explicit actions. PostgreSQL and remaining integration/browser cases were not run.

Positive evidence uses bounded English/taxonomy logic, not general semantic, multilingual or qualification verification. Supported conflicts need review; no raw career text is echoed in the added explanations and no career fact is silently changed.

After integration and fresh claim checks, continue with: full saved workspace-filter/visibility semantics for the preview (#118); evidence-backed education/certification contributions using existing reviewed-fact comparators (#114/#115/#118); controlled application/recovery acceptance when its current owner releases the relevant scope (#146). These are concrete candidates, not authority to overlap active claims.

No real ATS application was submitted; no hosted AI/CI/reviewer or new paid integration ran; no host firmware, driver, network or security configuration changed. New product acceptance used synthetic local fixtures.
