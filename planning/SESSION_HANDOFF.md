# Active CORE-WORKFLOW-TRIAL checkpoint — 2026-10-05, 10:44 America/Chicago

Verified GitHub `main` includes answer-bank PR #511 at `fb8ab73de6f666ed2644f9d1cc271eee25aef7f7` and F1 PR #512 at `2de5863da7418bf20087822d052b2beeb77ca64b`. F1 delivers profile-scoped job Save/Hide/Not Interested and job notes/history; its broader requirement remains PARTIAL. The current write branch is `codex/core-workflow-trial-application-pipeline-20261005`, based on that merge; draft PR #513 head `31b6053` is superseded after independent review found unscoped generated-document downloads. The local repair moves every generated download to the existing profile-scoped route and tests foreign-profile rejection. The separate Codespaces checkout remains preserved.

F2 connects the ten user-reported application pipeline stages and per-application notes/history through the existing append-only audit history. Applications board and detail UI retain canonical prepared-document links. “Applied” is explicitly reported by the user; operational application state and confirmation receipts are unchanged. No migration, external application request, private data or retained runtime was used. The profile-scoped generated-download repair passes the full 584-test Python suite in 324.939s, 31 affected integration/browser tests, 62 web tests and strict build. Fresh exact-head review and hosted evidence are pending. See `docs/evidence/2026-10-05-application-pipeline.json`.

Next: run the full suite on the profile-scoped download repair, publish explicit sanitized paths and verify the exact remote SHA, obtain fresh independent review, then require both exact-head push and pull_request Verification plus runtime artifact inspection/tree equality. Keep the PR draft and issues #196/#240 open until gates and full acceptance pass. Continue the authorized sequence after safe merge; feature cutoff is 13:00 America/Chicago / 18:00 UTC, after which stabilize only. Combined A-F journey and full requirements remain PARTIAL. External submission is OFF. Do not touch the retained private Windows runtime/database.

---

## Previous answer-bank checkpoint — 2026-10-05, 06:50 America/Chicago

GitHub main is `fb93ad48a3987c375350388ba8bf677db58575d2`. PR #511 is a draft on `codex/core-workflow-trial-answers-20261005`. The bounded E slice has question-aware answer snapshots and approval resets, exact-normalized suggestions with deliberate per-job review, scoped history/downloads, and immutable SHA-256 TXT exports. Independent review of candidate `315c2e1` found a delayed old API client without `expected_version` could restore an answer approval after a newer revocation. The current uncommitted repair requires that token for every existing-profile API update, returns 409 on missing/stale versions, and retains the transactional compare-and-swap for overlapping saves. The current full local suite passes 567 tests in 339.768 seconds; fresh focused browser/API flows, 62 web tests and build pass. Candidate `315c2e1` push and pull_request events (`37303429620`, `37303434314`) both passed, including required reports, but are superseded because of the independent review finding. The earlier d98 push failure (`37301337462`) was fixed by allowing 15 seconds for the same DRY_RUN_COMPLETE state while retaining STOP_BEFORE_SUBMIT assertions.

The strict-version repair is locally verified; source/evidence checkpoint still needs commit and push. Obtain a fresh independent exact-SHA review and both exact-head Verification events with all runtime reports before merge. Keep #511 draft until then. MJ-REQ-103..107 remain PARTIAL; exact-only matching, fixed confidence floor, sensitive/legal policy and broader requirements remain open. Original MJ-PR-028/029 dependencies remain intact. External submission is OFF; no private applicant data, retained runtime or product AI provider was used.

# Active CORE-WORKFLOW-TRIAL checkpoint — 2026-10-05

GitHub main is fb93ad48a3987c375350388ba8bf677db58575d2. PR #510 delivered the deterministic job-specific cover-letter slice and merged from candidate a63c79867923cb50b58c4cfde73d9ebb4c901413; its merge tree equals the candidate tree. Independent review found no actionable findings. Exact-head push Verification 37285052782 and pull_request Verification 37285059099, attempt 1, passed 554 Python tests and all required connected runtime steps. Both artifacts contained nine PASS reports and matched their GitHub SHA-256 digests. The bounded live Greenhouse read returned 18 jobs, zero external application requests, and a browser-safe missing-context block without creating an application/document. PR #510's closing receipt contains all SHAs, runs, artifacts, and results.

PR #509 merged as 959f9a36cd32a4c326459a0ba472a24a4abd7d3f; exact-head push 37275627438 and pull_request 37275632400 passed and its independent review/closing receipt is recorded. PR #508 manual capture remains merged as 77cf0825e51f34874f49cc7753e721b60a944ef0. PRs #504 and #506 remain merged and were not redone. The separate Codespaces checkout is preserved.

The root checkout is on codex/core-workflow-trial-answers-20261005 from verified main. The user confirmed GPT-6 Luna High and released the old client. Continue through exact review and CI; after merge, proceed to F manual tracking. Keep broader MJ-REQ-095 and MJ-REQ-100, the combined A-F journey, and full-spec release acceptance PARTIAL until their complete evidence exists. Product AI is not implemented. No private applicant data or retained Windows runtime was used.

At 13:00 America/Chicago on 2026-10-05, stop new features and stabilize through 15:00. Any unmet criterion remains PARTIAL.

---

# ChatGPT iteration handoff — 2026-10-04

## Current authorized milestone — 2026-10-05

The user superseded the prior stop-after-one-iteration instruction and
authorized the time-bounded **CORE-WORKFLOW-TRIAL** from current GitHub `main`.
Current task branch: `codex/core-workflow-trial-20261005`, based on
`44b98897fb1ced728ff206af2576b47c7cb240b1`. The root checkout was clean and
exclusive ownership was confirmed before branch creation; the separate
Codespaces checkout remains preserved. Model selection was user-confirmed as
GPT-6 Luna High; this interface cannot independently inspect billing metadata.
No provider API, private profile, retained Windows runtime or personal database
has been used.

PRs #504 (fact-review reset) and #506 (developer tooling) were verified merged
and were not redone. Existing profile/import/review and document-history
journeys passed focused browser regression checks. The first missing behavior,
issue #507, adds manual job capture to canonical job/posting/route/audit storage.
Its local synthetic browser/API/web acceptance is recorded in
`docs/evidence/2026-10-05-manual-job-capture.json`; it proves no pasted URL is
fetched, differentiates a reference from an application URL, verifies durable
readback/preparation handoff, and keeps external submission OFF. This is a
narrow `PARTIAL` slice, not completion of MJ-REQ-186/188/192 or of workflow B.
Independent review and exact-final-head push/PR CI with all runtime artifacts
are still required before merge. Queue entry MJ-PR-151 records the scope.

Continue the authorized sequence only after the exact-head gates pass. Next,
verify the existing supported public-source UI and perform a bounded actual
public Greenhouse GET separately from controlled fixtures, without starting or
updating the retained private runtime. Then prioritize missing user actions in
matching, preparation, reusable answers and manual tracking. At 13:00 America/
Chicago on 2026-10-05, stop new feature families and stabilize through 15:00.
Any unmet acceptance remains `PARTIAL`; the full 366-requirement release is not
the target. External submission remains OFF throughout.

The older handoff below is historical context; GitHub current `main` and the active user instruction supersede its prior stop and stale checkout observations.

## Current authorized milestone — 2026-10-05

The user authorized sequential implementation of **CORE-WORKFLOW-TRIAL** from current GitHub main, with new features stopping at 13:00 America/Chicago and stabilization through 15:00. Luna High was user-confirmed; this interface cannot inspect billing metadata. The root checkout is task-owned after the user confirmed the older client released. The separate Codespaces worktree remains preserved. No retained private runtime, private resume/profile, paid product AI, or external application submission has been used; external submission stays OFF.

PRs #504 and #506 remain merged and were not redone. PR #508 delivered the narrow manual capture fallback and merged as `77cf0825e51f34874f49cc7753e721b60a944ef0` from candidate `a05294cc1792b81924f01270852b1354b5d67505`. Its exact-head push Verification was run 37268232466; pull_request Verification was run 37268236341. Both passed required runtime reports. The PR #508 closing receipt also records a bounded public Greenhouse live read. Issue #507 was closed only for the narrow manual-capture gap; public-source UI proof and broader jobs requirements remain open.

Current branch `codex/core-workflow-trial-matching-20261005` starts from merged main `77cf0825e51f34874f49cc7753e721b60a944ef0`. PR #509 contains deterministic skill explanations from approved facts, profile-serialized rescoring, manual capture readback, and controlled mock classification. Reviews of candidates 5792dc5, 5937030, 6850cac, and 4328c95 found/repaired the prior issues; the independent review on 4328c95 itself reported no product defects. However, both 4328c95 hosted events had a hidden continue-on-error failure in `cli.ui_source_readback`: its exact text oracle still expected copy replaced by the matching UI. The separate public Greenhouse probe passed, but the browser readback report was missing, so neither event qualifies the head for merge. Source commit `db5f83ed077ed8478a7bfe0ec6e36b82c973718f` updates the runtime oracle to current copy. Local 281 integration tests, focused matching/flywheel tests, Chromium manual-capture, web tests/build, and repository validators passed on the prior matching source; the final candidate must repeat hosted checks. Evidence is `docs/evidence/2026-10-05-explainable-matching.json`; matching remains PARTIAL. Keep PR #509 draft until a fresh exact review and both exact-head events pass with all runtime reports, especially the conditional public-source UI readback. The optional AI dependency remains preserved; MJ-PR-152 is the deterministic slice. Record final candidate/base/run/artifact/merge IDs in the PR closing receipt, avoiding a self-referential commit.

After matching, finish the existing supported-source Jobs UI and confirm its bounded live-read behavior, then prioritize job-specific documents, reusable answers, and manual application history by the next user action they unblock. At 13:00 stop starting feature families and stabilize. Any missing acceptance remains PARTIAL; this milestone does not claim all 366 requirements or full release readiness.


The user requested that local delivery finish, publish its work, stop the local
runtime, and hand off for ChatGPT Web Pro iteration. No next feature was started.
Use latest GitHub `main`; old branch/session records are evidence, not ownership.
Start with [the copyable handoff prompt](../prompts/CHATGPT_WEB_HANDOFF.md).

## Verified implementation checkpoint

- Last implementation PR: [#501](https://github.com/MulattoTech/MJobs/pull/501).
- Tested candidate: `737d77732934fc903a416d71764eecc684ff5a25`.
- Implementation merge: `a0bf00690d6f9aa834e4e0001a3ae19efb35f055`.
- Exact-head Verification: push **37239204735 / job111544310647** and PR
  **37239207108 / job111544317989**, attempt 1. Both workflows and all 16 runtime
  artifacts passed, including connected contact-header UI and app-image tests.
- Local evidence: 534 Python regressions, 45 web tests/build, final focused fidelity
  checks, actual browser acceptance, 18 Linux app-image tests, independent review.
- [Exact receipt](../docs/evidence/2026-10-04-contact-header-exact-head-ci.json),
  [implementation evidence](../docs/evidence/2026-10-04-resume-contact-header.json),
  [all 15 session merges](../docs/evidence/2026-10-04-delivery-merge-index.json).

Final handoff publication [PR #502](https://github.com/MulattoTech/MJobs/pull/502)
contains documentation/planning/evidence plus a scoped personal-mode browser
locator repair discovered during final CI. Its own
final-head CI and merge receipt are recorded in that PR's closing comment. The
implementation checkpoint above is not a fixed future main SHA.

## Accepted and remaining work

**MJ-PR-024 and MJ-PR-025 are PASS**, backed by lifecycle/source-authority,
description-versioning, career/contact/preferences, native import, date review,
and bounded offline scanned-PDF OCR evidence. A merged prerequisite alone was
not treated as proof of remaining acceptance.

**MJ-REQ-092–094 and 096–099 are accepted**: immutable approved-fact history and
provenance, seven document collections, assertion gates, ATS layouts, consistent
DOCX/PDF/TXT, and generated/uploaded original comparisons with extraction/OCR
uncertainty. See the capability matrix and linked receipts.

**MJ-REQ-095 / issue 96 remains PARTIAL and OPEN.** Verified: skill ordering,
explicit approved-fact selection/omission, matched skill/technology emphasis,
and optional saved-contact headers. Broader summary/bullet rephrasing and
automatic keyword rewriting remain incomplete. MJ-PR-027's own requirements are
accepted, but its queue status remains PARTIAL because MJ-PR-026 is incomplete.

## Fact-editor regression #503 — accepted in PR504

PR504 merged as `b1691622a61ee368f058461ff002728325d275a9` from exact candidate `5f535d90f8f3b91e1a16bf1c5eb9a667b430d753`.
Text/source/kind edits reset both review flags. Three real browser journeys
cover persisted exclusion, verification-only exclusion, explicit reapproval,
stable identity, current TXT/DOCX/PDF and all historical document hashes.
Push and PR Verification each passed537 Python tests and all9 runtime reports;
independent static review found no actionable defects after its CI finding was
fixed. [Exact receipt](../docs/evidence/2026-10-04-fact-review-exact-head-ci.json).

The explicitly requested developer-tooling continuation is issue505: bounded
Codespaces/Codex CLI setup. Its offline bootstrap contract and shell checks
passed; actual devcontainer smoke, final-head Verification and independent
review remain required. The final exact-head and merge receipt belongs to the
[PR506 closing comment](https://github.com/MulattoTech/MJobs/pull/506), which supersedes this pre-merge snapshot. No automatic next iteration is authorized.
[Scoped evidence](../docs/evidence/2026-10-04-codespaces-bounded-iteration.json);
[new entrypoint](../prompts/CODESPACES_ITERATION.md).

The original Windows checkout is still clean at c89a09c and was not mutated or
deployed; an old client remained present, so this iteration used a new owned
clone at C:/Tools/Dev/MJobs-fact-review-503. Private files, retained volumes and
the separate prior Codespaces checkout were preserved. No billable Codespace
was created. No new product feature or autonomous continuation is authorized.

AI provider/task/cost-control work remains unimplemented. Two isolated small
local-model probes produced schema-valid but irrelevant selections and failed
the same oracle; [failure evidence](../docs/evidence/2026-10-04-local-ai-feasibility.json).
Do not relabel those probes as production AI acceptance.

## Local shutdown and preservation

- Main checkout: `C:/Tools/Dev/MJobs`. Closeout returns it to clean synchronized
  main and releases its private owner receipt.
- Eight mjobs project containers stopped gracefully. Database, documents, queue
  and mock-receipt volumes retained. Other applications and Docker daemon kept
  running. No production deployment occurred.
- Local feedback GUI stopped after verifying its module and checkout path; saved
  feedback/drafts preserved. Ports 18700–18703 have no listeners.
- Fifteen merged implementation branches retired locally/remotely after exact
  PR-head/ancestry checks; all commits remain in main.
- Seven unused test-image tags and isolated 1.88 GB model cache removed. No global
  Docker prune or unrelated model deletion.
- Ignored .env, private resumes/feedback/documents, outputs, dependency environments
  and diagnostic receipts retained locally and uncommitted.
- Independent clean `C:/Tools/Dev/MJobs-codespaces` preserved on
  `codex/codespaces-devcontainer-20261004`, observed at
  `9b0d508ad554b6ca847a73dec5b6ce797014fd10`. Reconcile its owner before changes.
- [Cleanup receipt](../docs/evidence/2026-10-04-session-closeout.json).

When local testing resumes, `./Start-MJobs.ps1` rebuilds and starts the existing
fictional mjobs workspace at 18700 with retained data. This is not PERSONAL mode.
Personal startup uses `-Personal` and a separate project; consult the workspace
mode runbook before switching. External submission stays OFF. Remote read-only
review does not require starting the local runtime.

Keep AGENTS.md, current issues/specs, IMPLEMENTATION_STATE, PR_QUEUE,
CAPABILITY_MATRIX and exact-head receipts authoritative. Older feature dashboards
are dated snapshots. No new implementation lane is active for this handoff.
