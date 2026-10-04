# Current delivery handoff - 2026-10-04

Root sole writer, branch `codex/approved-resume-selection-20261004`, baseline `b0c650b98d8c357202b4bdfc7fd2d1103ff3ea28`.
PR498 merged at exact tested tree; candidate61ca75cccd1a675f7bb8538a5c8b25253b60cd71.
Push37233961955/job111529281527 and PR37233976944/job111529323043 attempt1,
all16 artifacts and ATS browser/Linux assertions PASS. Receipt: `docs/evidence/2026-10-04-ats-headings-exact-head-ci.json`.
MJ097/issue98 closed. MJ-PR024/025 and MJ096/097/098/099 accepted.

Active MJ095/issue96 explicit approved resume fact selection. Private design:
`.git/afk-dev/next-approved-selection-plan.txt`. Preserve global profile, cover-letter
facts, legacy history, submission gates and exact template contracts. Bind UI
selection to observed profile version; strict API and locked worker validation.
None means all eligible; explicit empty/invalid selections must fail closed.
No new migration intended. Broader rephrasing remains PARTIAL.

Local implementation/review complete:526 Python tests zero skips,44 web/build,
18 PostgreSQL,10 actual Linux tests PASS. Empty UI selection creates no operation.
Evidence: docs/evidence/2026-10-04-approved-resume-selection.json. Next: publish,
verify final-head push+PR workflows/all8 artifacts each plus selection UI/runtime
assertions, merge safely and record partial MJ095 acceptance; do not close issue96.
No deployment or external submission. No owned test containers. Independent
Codespaces checkout and user services preserved. No current PR.
