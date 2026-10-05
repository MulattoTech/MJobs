> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

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
