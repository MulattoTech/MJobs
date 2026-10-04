# ChatGPT iteration handoff — 2026-10-04

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
contains documentation/planning/evidence only. Its own
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

## Next small iteration — not implemented

`apps/web/src/main.tsx:editFact()` retains review flags when an approved fact's
text, source or kind changes. Read-only review confirmed the UI review-boundary
gap. A future slice should reset verified/user_approved on actual changes to those
fields, preserve confidence-only/checkbox-only edits, and retain fact IDs/history.
First add real browser proof: Pending after edit, excluded after save, verification
alone still excluded, deliberate second approval restores eligibility. Explicit
API-supplied flags remain valid. No fix or passing regression is claimed here.

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
