# MJ-REQ-018 — Atom job destination integrity (scoped partial slice)

Run: `web-20261104T061555Z-3d76b2041394`
Claim: `refs/heads/webpro-claims/MJ-REQ-018` (owner commit `b6d5c52ef4d42564167805d686ee178e0fa2a7fc`)
Baseline protected main: `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf`
Issue: [#19](https://github.com/MulattoTech/MJobs/issues/19)
Status: **MJ-REQ-018 remains PARTIAL**.

## Reproduced defect

The first-party Atom parser iterated every entry child and assigned `row["link"]` for each `<link>`. An entry with a valid job-page alternate followed by an Atom `self` or `enclosure` link therefore emitted a feed-entry URL or attachment URL as the job destination. Reversing XML order changed the result; multiple distinct job alternates also silently selected the last one.

The initial new regression run on unmodified production source returned **4 failures, 6 passes, 4 passing subtests**, exit code 1. Two failures independently showed incorrect feed/attachment URLs, one showed silent acceptance of conflicting alternates, and one showed acceptance of entries containing only non-job links.

## Change and behavior

- For Atom entries, select the unique nonempty `rel=alternate` destination (including the Atom default when `rel` is omitted); ignore `self` and `enclosure`.
- Coalesce repeated identical alternate hrefs; reject zero or multiple distinct alternate destinations with the existing fixed `COMPANY_JOB_FEED_INVALID` category.
- Preserve RSS handling, JSON Feed handling, existing size/item/entity limits, downstream source policy, caller cancellation, and provider data provenance. No network request or application submission is introduced.
- Add deterministic synthetic cases for opposite XML orderings, conflicting alternates, non-job-only links, and repeated identical alternates.

## Focused verification

From the isolated worktree at the implementation revision:

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_company_job_endpoints.py tests/integration/test_verified_enrollment.py --tb=short
10 passed, 6 subtests passed, 0 failures, 0 skips, exit 0

python -m pytest -q -p no:cacheprovider tests/integration/test_company_feed_quality.py tests/unit/test_company_job_endpoints.py --tb=short
15 passed, 8 subtests passed, 0 failures, 0 skips, exit 0

python -m pytest -q -p no:cacheprovider tests/unit/test_company_job_endpoints.py tests/integration/test_verified_enrollment.py tests/integration/test_company_feed_quality.py --tb=short
16 passed, 8 subtests passed, 0 failures, 0 skips, exit 0 (final source after test-format edit)

python -m tools.verification.catalogue
PASS

python -m tools.verification.progress validate
PASS

python -m tools.verification.progress_dashboard --check
CURRENT

git diff --check
PASS
```

The successful pytest suites overlap; their pass counts must not be added to claim extra coverage. The initial failing source and its diagnostics were not deployed. This note records synthetic verification, not full CI, a real provider feed, public browser behavior, merge, deployment or literal requirement acceptance; those remain NOT RUN/PENDING until independently evidenced.

## Security and rollout

Ambiguous third-party link metadata now fails closed before persistence rather than fabricating a destination. Existing employer/domain admission remains authoritative; link selection is not employer identity verification. Scope is one parser, one test module and this evidence note. No migration or configuration change is required. The change is revertible as one source/test commit. The full eight-level source collection strategy and provider breadth remain incomplete.
