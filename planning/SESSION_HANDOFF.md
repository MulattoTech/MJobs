# Current delivery handoff — 2026-10-04

Sole writer: root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`, branch
`codex/local-resume-ocr-20261004`, based on main
`831fd5a91be9b9843b529bd6593978c4586750f8`.

PR485/486/488/489 merged with exact-head push/PR CI and runtime artifact receipts.
The latest receipt is `docs/evidence/2026-10-04-date-review-exact-head-ci.json`;
prior receipts are the profile, extraction and lifecycle exact-head evidence
files in the same directory. MJ-PR024 and MJ-REQ088/089/091 acceptance are evidenced;
issues86/87/88/89/90/92 are closed. Native extracted dates and responsibility
facts keep original text, stable IDs and explicit verification controls.

Current MJ-REQ090 slice adds native-first local scanned-PDF OCR in the supported
Linux Docker image using Poppler/Tesseract, bounded pages/pixels/memory/CPU/files/
time and private temporary files. No cloud OCR or silent correction. OCR can
misread SQL as SOL; the proof records that exact text, keeps confidence70 and
unverified/unapproved state, and verifies original bytes plus browser save/reload.
Missing engines remain NEEDS_OCR; blank documents are UNREADABLE. Native text is
preserved after unexpected OCR failures. Inline-only scans and same-page mixed
native/image text do not have complete coverage promised.

Final487 local tests (zero skips),38 frontend tests/build, real offline read-only
1GiB app-image API/Chromium proof and independent reviews passed. Browser fixtures
now retain high loopback sockets after an observed Chrome-blocked port1723 failure.
See `docs/evidence/2026-10-04-local-resume-ocr.json`.
Final-head push/PR workflows must both pass, including new mandatory
`ci-profile-ocr.json`, before merge and issue91 closeout. Exact receipts go in the
PR closeout and `.git/afk-dev/pr-<number>-exact-head.json` until the next tracked
slice. The session-owned image `mjobs-ocr-acceptance:20261004` is retained for
reproducibility; its short-lived containers used --rm and are gone.

After this gate, reconcile MJ-PR025 and inspect MJ-PR026/030 by acceptance evidence
before advancing dependent document functionality. Existing user Compose services,
private feedback and resumes are preserved. No external application submission,
remote resume transfer or production deployment is authorized.
