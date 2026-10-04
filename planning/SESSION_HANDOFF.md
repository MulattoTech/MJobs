# Current delivery handoff — 2026-10-04

Sole writer root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`, branch
`codex/document-provenance-20261004`, baseline main
`2120534c1d169c125412cf63321c2916d2a28984`.

PR485/486/488/489/490 merged with exact-head push/PR CI and runtime receipts.
MJ-PR024 and025 acceptance is evidenced; issues86–92 are closed. The latest
receipt is `docs/evidence/2026-10-04-ocr-exact-head-ci.json`: candidate
c7b6b37f9e200e367247540ab9a6b7ae57e77ce0, push37225046048 and PR37225050413,
attempt1, all seven artifacts PASS in each. OCR remains bounded Linux/English,
image-only pages with XObjects; uncertain text requires explicit user review.

Current MJ-PR026 slice adds actual document timestamps/method, original fact
provenance, PDF/DOCX/TXT consistency and browser checksum-backed downloads.
Migration af2049e17b63 (parent e9024a1b7c30) preserves legacy unknowns and refuses
populated provenance loss. 490 Python tests zero skips,40 web/build,11 isolated
PostgreSQL tests, real browser and independent review PASS. See
`docs/evidence/2026-10-04-document-provenance.json`. Exact final-head hosted
workflows and seven artifacts each remain the merge gate. Issues93/95/99 remain
open until that gate; library issue94 and tailoring issue96 remain incomplete.

After merge, reconcile receipts and complete useful document library/tailoring
work by acceptance, reusing current immutable storage and generation. Retain
private data and the independently owned Codespaces worktree. Test PostgreSQL
container removed after identity check; user services untouched. Session-owned
OCR image remains available. External application submission is OFF; no production
deployment or private resume transfer is authorized.
