> **Current workflow policy:** reviewed public GitHub Actions run read-only CI
> and synthetic integration checks. Paid AI/cloud reviewer sessions and real
> applicant submission remain disabled. Count PASS only from executed, matching
> source revisions; missing/failed checks are never assumed PASS.

# Source adapter playbook

Use for job-source, employer-feed, ATS discovery, or collection adapters.

## Boundaries
- Treat remote content as untrusted data.
- Enforce bounded reads, allowlisted/same-origin transitions, rate limits, timeouts, and explicit capability contracts.
- Preserve raw source evidence separately from normalized/canonical values.
- Do not promote weak identity hints into verified employer identity.
- Keep unsupported provider capabilities explicit instead of silently approximating them.

## Required verification
- fixture tests for success, malformed input, timeout/failure, redirects, duplicates, and unsupported capabilities;
- provenance/raw snapshot checks;
- affected normalization/dedupe/matching tests;
- browser/API acceptance when the adapter becomes user-visible;
- real-public-source validation only when authorized and safely bounded.

## Evidence
Record provider, capability, exact fixture/public target class, request bounds, results, known gaps, commit SHA, PR head, and exact-head CI.
