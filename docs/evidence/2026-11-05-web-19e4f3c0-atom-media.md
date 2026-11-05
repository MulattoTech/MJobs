# MJ-REQ-018 — Atom alternate media policy and discovery persistence

Run: `web-20261105T000133Z-19e4f3c0fa09`.
PR: [#929](https://github.com/MulattoTech/MJobs/pull/929).
Requirement: [MJ-REQ-018 / #19](https://github.com/MulattoTech/MJobs/issues/19), still **PARTIAL**.
Atomic objective: `refs/heads/webpro-claims/PR-929-INTEGRATION`,
owner `6bf0fdc4eef10c1f57fdbdc0328d0d461539c058` (REST 201 Created and exact read-back).
Protected baseline: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
Updated PR baseline: `a15d02eecfa94029e6d06372f349a5948746a461`.

## Contract and correction

The prior review on PR #929 identified that a PDF marked `rel=alternate` could
still become an application route. Inspection confirmed that downstream feed
normalization validates URL structure, not Atom media metadata. In addition,
a valid HTML job page plus a PDF alternate was rejected as ambiguous.

Atom job destinations now consider undeclared media types (backwards-compatible),
`text/html`, and `application/xhtml+xml`; case, surrounding whitespace and media
parameters are normalized. Explicit PDF/feed/JSON/plain-text/image media types
cannot become candidate job pages. With no remaining destination, the existing
fixed `COMPANY_JOB_FEED_INVALID` result applies. Distinct HTML/XHTML destinations
remain ambiguous; relation handling, duplicate links, RSS and JSON Feed are unchanged.

Media declarations are advisory. This policy does not verify actual response
content or employer legitimacy, and no new network fetch is introduced.

## Executed evidence

- RED on unchanged parser at the updated PR baseline: the new unit cases produced
  **13 failed subtests**, 12 parent tests reported passed and 6 existing passing
  subtests; pytest exit 1. Five non-page declarations were accepted and eight
  HTML/untyped-plus-PDF combinations failed to select the job page.
- GREEN unit module after correction: **12 passed, 19 subtests passed**, exit 0.
- Connected focused validation: **21 passed, 25 subtests passed**, exit 0, 21.12 s:

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_company_job_endpoints.py tests/integration/test_company_atom_destinations.py tests/integration/test_company_feed_quality.py tests/integration/test_verified_enrollment.py --tb=short
```

The connected run used the existing project virtual-environment interpreter
read-only, from the isolated worktree. The first system-Python attempt stopped
during collection because installed pydantic-core 2.50.3 did not match that
interpreter's Pydantic requirement (2.50.2). No global packages were changed.

The new integration fixture executes real discovery, parsing, source enrollment
and job persistence with a synthetic HTTP transport and disposable SQLite:
a valid HTML destination persists once across link reordering/repeat discovery;
the PDF is never fetched; an invalid entry prevents enrollment and all partial
job/route writes, including when a valid entry precedes it.

Catalogue validation, progress-journal validation, dashboard consistency, and
`git diff --check`: **PASS**. Counts above overlap and must not be added.

## Delivery limits

This source change needs final-revision protected CI and serial integration.
Production, browser checks, and full eight-stage collection-strategy acceptance
are **NOT RUN/PENDING** at this checkpoint. The existing review raised the media
policy gap; this session's follow-up review is self-review, not an independent
approval of its own patch.

No schema/configuration migration, real employer request, application submission,
personal data, model invocation, quota change or canonical acceptance promotion.
Rollback is a protected revert of the scoped PR; no data-destructive rollback is
required. Existing incorrectly collected rows are not retroactively rewritten.

## Required history-scan follow-up

The final product commit passed the focused suite again: 21 tests and 25 subtests,
exit 0 (111.37 seconds). Its hosted history-wide secret scan failed in
[run 41939659092](https://github.com/MulattoTech/MJobs/actions/runs/41939659092);
redacted artifact 13375696574 contains exactly two findings in PR #937's evidence
note at lines 45/46, not in this PR's product files. This failed run is retained.

I independently retrieved each cited test file at the reported historical commit
through GitHub. Both reported values equal its exact Git blob object ID. The
paths are `tests/integration/test_browser_job_extraction_api.py` and
`tests/integration/test_cheap_prefilter_api_scope_security.py`. These are public
repository object identities. The separate owner already published the exact
fingerprint pair, and other reviewing sessions confirmed the same disposition.

Under a fresh short `SECRET-SCAN-FINGERPRINTS` claim (HTTP 201 and matching
read-back), this branch reuses that identical two-entry `.gitleaksignore` delta.
No wildcard, rule/path exemption, scan-scope change or required-check change is
introduced. The Atom parser and regression-test blobs are unchanged. The original
scan failure is not relabeled; a fresh required scan must pass on the new head.
