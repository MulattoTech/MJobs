# Current delivery handoff — 2026-10-04

Sole writer root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`, branch
`codex/approved-skill-order-20261004`, baseline main
`49d71a9c99b045e8bb76b2d2ae136c4f52a49454`.

PR485/486/488/489/490/491/492 merged with exact-head push/PR CI and runtime receipts.
MJ-PR024/025 plus MJ-REQ092/093/094/098 accepted; issues86–95 and99 closed.
Latest receipt: `docs/evidence/2026-10-04-document-library-exact-head-ci.json`,
candidate618878db34e8369247069a648e71f259f852d13d, push37227534862 and
PR37227539003,attempt1,all seven artifacts each PASS. The library stores all
seven types without approving facts or mislabeling generic generation.

Current slice implements explicit opt-in skill ordering through API/worker/UI.
Only approved skill facts move within their existing slots; all wording and
non-skill history stay exact. Generic output remains available. Original target
snapshot, matching and ordered IDs are recorded with distinct method/template.
Mode/target/profile changes create allowed-state revisions; identical requests
are idempotent; confirmed/active history stays locked. Migration cf4216a93b85
(parent be3105f82a74) refuses loss of recorded context and preserves legacy unknowns.

500 Python tests zero skips,40 web/build,13 isolated PostgreSQL, real Chromium and
independent review PASS. `docs/evidence/2026-10-04-approved-skill-order.json`.
Exact final-head hosted push/PR workflows and seven artifacts each remain the
merge gate. Hosted UI proof now includes the explicit option through the worker.
MJ-REQ095/issue96 remains PARTIAL even after merge: summary adjustment, rephrasing,
bullet selection/removal and broader keyword alignment are not implemented.

After merge, reconcile exact receipts and choose the next coherent document
safety/quality gap by acceptance (MJ-REQ096/097 are candidates). Preserve all
private data, independently owned Codespaces checkout and user services. Owned
PostgreSQL test container removed. External submission OFF; no production
deployment or private resume transfer is authorized.
