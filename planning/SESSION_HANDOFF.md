# Current delivery handoff — 2026-10-04

Sole writer root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`, branch
`codex/document-library-20261004`, baseline main
`36d28ba62f81ae1acc1a5d6a4f0e7950d6fd9550`.

PR485/486/488/489/490/491 merged with exact-head push/PR CI and runtime receipts.
MJ-PR024/025 and MJ-REQ092/094/098 accepted; issues86–93,95,99 closed. Latest receipt:
`docs/evidence/2026-10-04-document-provenance-exact-head-ci.json`, candidate
878cbc0b7e48de54b8eb76f30d12d7821cadd8bd, push37226186860 and PR37226189890,
attempt1; all seven artifacts PASS each, including connected provenance/TXT UI.

Current MJ-REQ093 slice stores all seven document types in immutable versioned
collections backed by shared original blobs. Base IDs/history preserved; base
import requires BASE_RESUME membership; library downloads require profile-owned
revision membership and checksum. Generated generic resumes remain honest
GENERATED_RESUME records with job/company context. Uploads never approve facts.
Migration be3105f82a74 (parent af2049e17b63) refuses loss of non-base collection kinds.

495 Python tests zero skips,40 web/build,11 isolated PostgreSQL, real Chromium
seven-kind store/version/reload/download and independent review PASS. See
`docs/evidence/2026-10-04-document-library.json`. Exact final-head hosted workflows
and seven artifacts each remain required before merge/issue94 closeout. New hosted
UI check stores/downloads certification on PostgreSQL without importing career facts.

Next: reconcile merge receipts, then deliver opt-in deterministic tailoring using
approved facts only, preserving original wording and generic preparation. Issue96
remains open. No automatic rewrite or unsupported claims. User services/private
files and independently owned Codespaces checkout preserved. Owned temporary PG
container removed. External submission OFF; no production deployment or private
resume transfer is authorized.
