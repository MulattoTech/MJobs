# Agent efficiency audit

Date: 2026-11-19
Repository: `MulattoTech/MJobs`
Audit branch: `mulattotech-agent-efficiency-optimization`
Starting HEAD: `81ca391b474d288582d51ee7d9db8a2dbb189153`
Mode: authorized local inspection and focused implementation; no merge, deploy, production-data access, or live application submission.

## Execution facts

The starting worktree was clean. The configured remote is `origin` at
`MulattoTech/MJobs`; the default/integration branch is `main`. GitHub access
was available through `gh`, and 20 open PRs were inspected. No PR used this
branch, and no existing issue was identified for this housekeeping request, so
the change remains a small reviewable branch without issue-acceptance claims.

The current checkout was inspectable. Cloudflare, browser, MTerm-CLI,
production services, and private provider endpoints were not used or required.
Historical journals and large evidence files were not loaded as source context.

## Scorecard

| Area | Score | Evidence |
| --- | ---: | --- |
| Agent ergonomics | 4/5 | `docs/REPO_MAP.md`, scoped area guides, and `tools.agent_context` provide bounded entry points. |
| Context efficiency | 2/5 | The measured onboarding manifest is 105,358 bytes, about 26,339 tokens by the repository heuristic; `planning/REQUIREMENT_INDEX.json` alone is 77,470 bytes. |
| Verification speed | 3/5 | Focused agent-context tests complete in 12.71–15.81 seconds; catalogue and progress validation both pass quickly, while full web/integration checks are broader and service-dependent. |
| Architecture/contracts | 3/5 | The repository map gives clear ownership, but large API/UI entrypoints and several hand-maintained planning authorities remain costly to navigate. |
| Multi-agent conflict safety | 3/5 | Protected-main and single-integration-owner rules are explicit, but shared planning/queue files and 20 open PRs create serialization pressure. |
| Reliability/security | 4/5 | Real submissions, access-control bypasses, production mutation, and Fleet-controller duplication are explicitly guarded; hosted exact-head evidence remains separate from local checks. |

## Measured baseline

Largest tracked files by bytes included:

| File | Bytes | Lines |
| --- | ---: | ---: |
| `planning/ISSUE_SEED.jsonl` | 512,455 | 367 |
| `planning/IMPLEMENTATION_STATE.json` | 460,405 | 5,867 |
| `planning/PR_QUEUE.json` | 393,541 | 6,381 |
| `planning/CAPABILITY_MATRIX.json` | 290,158 | 4,437 |
| `apps/web/src/main.tsx` | 266,781 | 2,063 |
| `apps/api/main.py` | 162,463 | 2,878 |
| `cli/ui_e2e.py` | 187,674 | 2,993 |

The top recent churn hotspots by file occurrence in non-merge history were
`planning/IMPLEMENTATION_STATE.json` (583), `planning/PR_QUEUE.json` (475),
`planning/SESSION_HANDOFF.md` (452), `planning/CAPABILITY_MATRIX.json` (355),
`apps/web/src/main.tsx` (210), and `apps/api/main.py` (203). These are
collision indicators, not proof that each file needs decomposition.

The core guidance files plus the full session handoff total 117,156 bytes,
about 29,289 heuristic tokens. The new bounded bootstrap manifest covers
9 existing onboarding files and reports 105,358 bytes / 26,339 heuristic
tokens without reading their contents. The estimate is deliberately only
`bytes / 4`; it is not a model-tokenization benchmark.

CI contains 13 workflow files: browser regression, general CI, Codespaces,
connected acceptance, dependency review, feature studio, Fleet control,
integration, deployment candidate, Pages, release, secret scan, and verified
review are present as separate workflow concerns (some files contain multiple
checks). Local catalogue and progress validation passed. The focused Python
test baseline was 24 passed, 1 skipped, and 462 subtests; three runs took
15.81s, 13.74s, and 12.71s on this Windows checkout.

## Guardrails and ownership

Do not casually edit `planning/CAPABILITY_MATRIX.json`,
`planning/REFERENCE_GAP_MATRIX.json`, `planning/IMPLEMENTATION_STATE.json`,
`planning/PR_QUEUE.json`, generated requirement indexes, or the shared
session handoff. They are acceptance, queue, or continuity authorities with
single-writer expectations. Do not duplicate Fleet controller code; its source
belongs to `MulattoTech/Fleet-Control-Hub`. Keep external application
submission OFF and use synthetic fixtures for provider behavior.

The exact hosted CI revision remains the publication authority. Local PASS
results do not prove hosted acceptance, human review, production readiness, or
real-provider connectivity.

## Ranked backlog

### Tier A — quick wins

1. **A1 — Bootstrap context budget manifest (implemented).** Add bounded file
   sizes and heuristic token totals to `tools.agent_context bootstrap`.
   Evidence is direct; effort is low, risk is low, reversible by removing one
   additive field. Verification: the two agent-context unit modules, catalogue,
   progress validation, and `git diff --check`.
2. **A2 — Add a changed-file verification profile.** Reuse existing catalogue,
   progress, and language-specific checks behind an explicit read-only command
   that accepts changed paths and never rewrites planning state. This would
   reduce repeated command discovery; it depends on mapping current CI
   selectors first. Falsifier: if changed-file selection routinely misses
   cross-cutting contracts, keep the current explicit commands.
3. **A3 — Generate a compact ownership index from `docs/REPO_MAP.md` and area
   contracts.** Keep the source documentation authoritative and check only
   freshness, rather than adding another hand-maintained registry. Falsifier:
   if ownership remains too ambiguous to parse deterministically, improve the
   map instead of generating stale data.

### Tier B — structural improvements

1. **B1 — Split `apps/web/src/main.tsx` by route/UI cluster behind
   characterization tests.** Preserve route behavior and isolate seams before
   moving code. This is medium effort and high conflict risk because the file
   is a churn hotspot.
2. **B2 — Split `apps/api/main.py` by router/bootstrap concerns.** Generate or
   validate API contract surfaces at the boundary. Do not do this by line
   count alone; first capture import, side-effect, and startup behavior.
3. **B3 — Replace append-heavy session handoff history with compact immutable
   checkpoints plus a generated current summary.** This must preserve recovery
   semantics and be serialized with the integration owner.

### Tier C — capability-unlocking refactors

1. **C1 — Generated contract/index pipeline.** Generate route-to-handler,
   service-to-test, and API-client maps from source metadata, with CI freshness
   checks. This could unlock faster agent routing and drift detection but
   introduces generator/toolchain maintenance.
2. **C2 — Ownership-aware PR collision reporting.** Compare changed paths from
   open PRs against protected shared files and report likely serialization
   conflicts without blocking independent work. This unlocks safer concurrency
   but depends on stable GitHub permissions and conservative false-positive
   handling.
3. **C3 — Contract tests across persistence, API schemas, and web clients.**
   This would reduce handwritten drift and unlock safer parallel feature work,
   but requires staged compatibility policy and broader CI cost.

## Alternatives and unlocked capabilities

| Subsystem | Keep + optimize | Targeted refactor | Fundamentally different design |
| --- | --- | --- | --- |
| Agent onboarding | Keep `agent_context`; expose bounded measurements (implemented). | Generate a compact manifest and freshness check from guidance files. | Replace file guidance with a service/catalogue; rejected now because it adds runtime dependency and loses local portability. |
| Verification | Keep explicit commands and improve changed-file selection. | Add a read-only profile that composes existing validators. | Remote-only verification; rejected because it weakens offline recovery and exact local diagnosis. |
| Shared planning state | Keep single-writer JSON authorities and improve navigation. | Generate read-only indexes from canonical matrices. | Distributed database/event log; rejected now because migration and recovery cost exceed measured need. |
| Large entrypoints | Keep with characterization tests. | Extract cohesive route/bootstrap clusters. | Plugin/event architecture; attractive for extensibility, but too much blast radius without a concrete integration requirement. |

## Implementation result

Implemented A1 in:

- `tools/agent_context.py`: additive `CONTEXT_MANIFEST` and read-only
  `context_manifest()` measurements included in bootstrap output.
- `tests/unit/test_agent_context.py`: tests existing-file measurement and
  bootstrap exposure.
- `docs/AI_AGENT_CONTINUITY.md`: documents the field and its heuristic limits.

The implementation does not read large file contents, mutate planning state,
change routing decisions, or alter existing checkpoint formats. Work is
performed by GitHub Copilot Fusion using GPT-5.6 Luna. Local checks performed
on the implementation branch before publication:

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/unit/test_agent_context.py tests/unit/test_agent_context_child_ids.py` | PASS: 24 passed, 1 skipped, 462 subtests |
| Same focused command, three timing runs | PASS: 15.81s / 13.74s / 12.71s |
| `python -m tools.verification.catalogue` | PASS |
| `python -m tools.verification.progress validate` | PASS |
| `python -m tools.verification.progress_dashboard --check` | CURRENT |
| `python -m tools.verification.commit_provenance --commit HEAD` | PASS |
| `git diff --check` | PASS |
| Full unit suite, frontend tests/build, integration/browser/Compose checks | NOT_RUN; outside this Python-only slice |
| Hosted exact-head checks | NOT_RUN; no PR was created in this audit |

## Conflict-safe roadmap

1. Land A1 as an isolated tooling/documentation PR.
2. Independently inventory CI selectors and implement A2 without touching
   acceptance matrices.
3. Generate A3 from existing ownership docs; serialize only the generated
   artifact/freshness check.
4. Add characterization tests for `apps/web/src/main.tsx`; only then split one
   cohesive UI cluster.
5. Add characterization tests for `apps/api/main.py`; split router/bootstrap
   concerns in a separate PR.
6. Design B3 with a recovery fixture and migration/rollback proof before
   changing handoff persistence.
7. Treat C1–C3 as staged design proposals requiring explicit review.

Items 2–3 can proceed concurrently if they avoid shared planning files. Items
4–6 require serialization around their respective high-churn entrypoints and
continuity authorities. Roll back any optimization that increases false
routing, loses recovery data, or raises targeted-check time without a measured
capability gain.

## Continuity handoff

Starting HEAD was `81ca391b474d288582d51ee7d9db8a2dbb189153`. The implementation
and audit follow-up are committed on this worktree branch; the PR head supplies
the exact final revision for hosted verification. The working tree must remain
clean after attribution correction and publication. No issue or PR is linked to
this housekeeping slice. Reproduce the focused verification with the commands
in the implementation table above.
