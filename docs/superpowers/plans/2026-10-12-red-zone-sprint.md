# Mission Control red-zone implementation plan

**Goal:** Deliver verified integrated functionality only in the nine user-selected areas, with application automation and matching AI receiving the temporary delivery emphasis regardless of P3/P4 catalogue labels.

**Architecture:** Extend existing scoped APIs, durable workers, application attempts and value-free review receipts. Use synthetic browser flows and existing persistence; no parallel integration path or external application submission.

**Stack:** FastAPI, SQLAlchemy/PostgreSQL, existing operation outbox, Playwright, React/TypeScript.

**Specs:** `docs/spec/areas/{application_automation,test_harness,matching_ai,delivery_control,orchestration_data_security,operations_ux_ci,docs_verification,web_api_analytics,company_ats_discovery}.md`.

## Reconciled baseline

Root checkout remains on `test/employer-identity-postgres-56-20261013` at `50cff447dcaf80c8bf6ec83afa299594656882d0`. Its search-history panel, old release plan and handoff dirt are preserved. PR597 is merged. PR611 was independently checked: seven reviewed-identity PostgreSQL cases executed/pass; all seven workflow runs passed, including full browser and Compose. Protected integration produced `e76d3898972ac754db74a30b34bf95cc92e69948`. The sprint worktree starts there. Dependency PRs 598/536/531/521/520/519 remain untouched.

Both authoritative matrices were read and joined to all 425 compact-index entries; zero status discrepancies. No screenshot percentage was used and no acceptance was promoted.

| Allowed area | PASS | PARTIAL | NOT_RUN | Total |
|---|---:|---:|---:|---:|
| application_automation | 0 | 18 | 33 | 51 |
| delivery_control | 0 | 1 | 10 | 11 |
| test_harness | 1 | 6 | 16 | 23 |
| matching_ai | 2 | 10 | 8 | 20 |
| docs_verification | 1 | 1 | 7 | 9 |
| web_api_analytics | 4 | 14 | 19 | 37 |
| company_ats_discovery | 10 | 21 | 23 | 54 |
| orchestration_data_security | 5 | 14 | 8 | 27 |
| operations_ux_ci | 10 | 8 | 17 | 35 |

Allowed-area P0: 0/11 accepted (1 PARTIAL, 10 NOT_RUN). Allowed-area P1: 17/91 accepted (29 PARTIAL, 45 NOT_RUN). Original and additive denominators remain distinct in the canonical authorities.

## Ranked implementation shortlist

### 1. Explicitly reviewed conditional-field dry-run recovery — selected

- Requirements: MJ-REQ-121/#122 PARTIAL; MJ-REQ-133/#134 PARTIAL; MJ-REQ-135/#136 PARTIAL; MJ-REQ-208/#209 PARTIAL; MJ-REQ-156/#157 PARTIAL; MJ-REQ-158/#159 PARTIAL. Supports MJ-REQ-213/#214 PARTIAL and P0 MJ-REQ-292/#293 PARTIAL without claiming their whole acceptance.
- Areas: application_automation, test_harness, orchestration_data_security; delivery_control evidence gate.
- Outcome: a sourced synthetic job with prepared verified documents reaches a newly required known conditional field, pauses, presents the actual approved value for explicit review, then safely reruns to the local review page and records DRY_RUN_COMPLETE. Reload/restart retain review and outcome. Unknown or changed fields still stop.
- Actual gap: `mock_adapter._inspect_dynamic_controls` always blocks newly required controls, even known mapped ones. The standalone dynamic-state evaluator and local UI do not authorize worker continuation. The separate controlled Greenhouse/Lever fixture already works but is one-page and is not provider acceptance.
- Sources: `services/applications/mock_adapter.py`, new `services/applications/mock_dynamic_review.py`, `services/applications/rehearsal_store.py` (reuse only), `services/applications/attempt_history.py` (reuse input identity), new `apps/api/mock_dynamic_review.py`, `apps/api/main.py` router mount, new `apps/web/src/mock-dynamic-review.tsx`, `apps/web/src/mock-run-controls.tsx` mount. Existing `fixtures/mock_server.py` known/unknown conditional flows are sufficient.
- Acceptance: no review means no advance; reviewed exact known dropdown reaches dry-run result; no submit POST; input/metadata/options/route/document/profile change invalidates review; foreign scope denied; review request replay is idempotent; browser/API/store reopen retains result; worker request replay opens no second browser; submit=true cannot consume dry-run approval.
- Tests: new focused service/API tests, actual synthetic browser/worker flow, UI consent/stale-response tests; adjacent mock application, run-request, preflight and document-integrity suites; exact-head full browser/Compose/PG CI.
- Dependencies/conflicts: PR611 resolved first. Existing old claims for form-analysis/dynamic-workspace paths and Greenhouse adapter branch are avoided. No changes to those paths, answers service, profile implementation or shared migrations. Root owns the one implementation worktree and integration.
- Impact: concrete application/harness gap removed; no guaranteed PASS count because generic dynamic forms, other providers and full-stack acceptance exceed this bounded slice. Six named parent requirements remain PARTIAL unless their full contracts are separately proven.

### 2. Saved-search-scoped durable AI evaluation

- Requirements: MJ-REQ-108/#109, 109/#110, 110/#111, 111/#112, 112/#113, 114/#115 all PARTIAL (P3); MJ-REQ-271/#272 NOT_RUN (P1); 156/#157 PARTIAL (P1).
- Areas: matching_ai, orchestration_data_security, operations_ux_ci, web_api_analytics.
- Outcome: explicitly evaluate only the selected owned search's candidates with its actual scoring criteria, preserving readable search/score provenance in durable advisory results. Provider outage leaves deterministic discovery working.
- Gap: `services/ai/durable_evaluation.py:queue_evaluation` and the cost-plan preview in `apps/api/main.py` currently use profile-wide persisted scores; neither accepts selected-search scope. Reuse `services/matching/selected_search_scoring.py`, `services/discovery/matching.py`, `services/ai/cost_controls.py`, `services/ai/tasks.py`, `apps/web/src/durable-ai-evaluation.tsx`.
- Tests: opposing saved searches produce different bounded candidate sets; unauthorized/disabled/stale search makes zero calls; immutable snapshots and request replay; synthetic structured provider failures, no partial publication, unchanged deterministic scores; UI reload shows saved scope. Extend `tests/integration/test_durable_ai_evaluation.py` and its PostgreSQL peer.
- Dependencies/conflicts: existing selected-search scorer is a read dependency, not general core-search expansion. Serialize API main and shared planning with slice1. Avoid old matching worktree's owned paths until reconciled.
- Impact: advances six lower-labelled matching parents plus P1 operation/outage acceptance. No live paid inference; provider-specific acceptance remains NOT_RUN.

### 3. Interrupted application recovery with concurrent-worker proof

- Requirements: MJ-REQ-143/#144, 144/#145, 145/#146, 158/#159 PARTIAL; MJ-REQ-221/#222 NOT_RUN (P1); MJ-REQ-274/#275 PARTIAL (P1).
- Areas: application_automation, test_harness, orchestration_data_security, operations_ux_ci.
- Outcome: users can distinguish safe retry from uncertain submission after a worker/browser interruption; read-only receipt reconciliation prevents duplicate physical attempts.
- Sources: `services/applications/confirmation_recovery.py`, `run_requests.py`, `attempt_history.py`, `apps/api/application_confirmation_recovery.py`, `apps/web/src/application-confirmation-recovery.tsx`, `workers/tasks.py`.
- Existing implementation includes durable attempts and reconciliation; start by reproducing an uncovered interruption boundary, not rebuilding it. Tests: `tests/integration/test_application_worker_crash_postgres.py`, `test_application_confirmation_recovered_attempt.py`, `tests/browser/test_application_confirmation_recovery.py`; add real two-worker reservation and receipt-loss negative controls.
- Dependencies/conflicts: shares worker/attempt lifecycle with slice1; serialize. Requires disposable PostgreSQL for concurrency proof. Count MJ-REQ-221 only if the complete two-worker invariant passes; parent recovery remains partial where other operation kinds are unproven.

### 4. Durable per-queue automation pause/resume

- Requirements: MJ-REQ-152/#153 and 153/#154 NOT_RUN (P4); MJ-REQ-155/#156 and 158/#159 PARTIAL (P1); MJ-REQ-199/#200 NOT_RUN.
- Areas: application_automation, orchestration_data_security, web_api_analytics, operations_ux_ci.
- Outcome: users can pause preparation and AI queues without losing queued intent, observe the saved state after restart, and deliberately resume without duplicate execution.
- Sources/contracts: `workers/dispatch.py`, `workers/tasks.py`, existing scoped operation list/cancel routes in `apps/api/main.py`, `apps/web/src/mock-run-controls.tsx`; settings/persistence interface must be defined before any schema edit.
- Tests: pause before dispatch, pause during safe execution, restart, resume once, owner isolation, AI provider unknown-outcome and application SUBMITTING fences. Do not reinterpret cancellation as a complete pause implementation.
- Dependencies/conflicts: shared worker/persistence contracts require serialized ownership after slices1/2. Broader and less bounded than first slice; exact missing switch coverage must be established before coding.
- Impact: potentially adds application-control acceptance and advances continuous-operation P1 gates; no guaranteed whole-requirement closure for all six global switches/queue/source/company scopes.

### 5. Connected workflow fault-injection acceptance harness

- Requirements: MJ-REQ-208/#209, 209/#210, 210/#211, 211/#212, 213/#214 PARTIAL; MJ-REQ-224/#225 NOT_RUN; MJ-REQ-288/#289 PARTIAL (all P1). P0 291/#292 NOT_RUN and 292/#293 PARTIAL remain gates.
- Areas: test_harness, docs_verification, delivery_control plus tested application/AI/orchestration surfaces.
- Outcome: one reproducible disposable journey exposes actual discovered-job → approved preparation → dry-run/review → receipt/restart failures, with executable failure injection and per-step truthful results.
- Sources: `fixtures/mock_server.py`, `cli/verify_readiness.py`, `cli/compose_recovery.py`, existing browser and application integration modules; verify exact executable paths before claiming writes.
- Tests: changed mandatory field, invalid document hash, database outage, browser crash, unknown provider result, duplicate delivery; no off-origin request and no external submission. Reuse existing Compose/browser harness, never create a cosmetic parallel dashboard.
- Dependencies/conflicts: depends on product paths from slices1–3; shares workflow files with the now-merged PR611. Existing passing CI is reused as evidence, not called new functionality. Implement only uncovered executable harness behavior.
- Impact: strongest route to legitimate P1 test acceptance; P0 umbrella acceptance requires every original criterion, including unrelated prerequisites, so no scoped-sprint closure promise.

## First-slice execution contract

- [x] RED: prove known conditional field cannot currently complete a reviewed dry-run.
- [x] Build a value-free observation fingerprint using the existing application input identity plus actual bounded field metadata; resolve only approved exact mapped answers.
- [x] Reuse durable rehearsal acknowledgement for explicit owner-scoped review; expose transient values only in the scoped response.
- [x] Revalidate current DOM, profile, route, document and review identity in the worker; only dry-run may fill supported reviewed conditional dropdowns. All other states preserve existing fail-closed review.
- [x] Mount the review UI beside existing mock-run controls, preserving explicit new run intent and lost-reply recovery.
- [ ] Prove connected reload/restart, stale inputs, unknown fields, duplicate requests and zero submission; run adjacent regression and exact-revision CI.
- [ ] Publish one reviewable PR, inspect artifacts, integrate only through repository rules, then reconcile acceptance.

No priority edits, historical-claim deletion, autonomous loop restart, paid model invocation or external job submission. One integration owner. Source rollback is a normal revert; existing append-only audit receipts remain harmless without this consumer. New acceptance never follows solely from a commit or green CI.
