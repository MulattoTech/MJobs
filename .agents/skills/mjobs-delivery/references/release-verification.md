> **Current workflow policy:** reviewed public GitHub Actions run read-only CI
> and synthetic integration checks. Paid AI/cloud reviewer sessions and real
> applicant submission remain disabled. Count PASS only from executed, matching
> source revisions; missing/failed checks are never assumed PASS.

# Release verification playbook

Use before merge, release-readiness claims, or when repairing CI/release gates.

## Exact-head rule
A local PASS and a prior-head green workflow are historical evidence only. Resolve the PR's current head SHA and inspect checks/workflows for that exact SHA.

## Required verification
Run the broadest practical affected gate:
- focused and neighboring tests;
- full Python regression when practical;
- frontend tests/build for web changes;
- browser acceptance for user-visible paths;
- migration/runtime checks for persistence changes;
- `python -m tools.verification.agent_skills`;
- `python -m tools.verification.catalogue`;
- `python -m tools.verification.progress validate`;
- `git diff --check`.

If a browser test skipped only because the web build was absent, build and rerun it.

## Evidence
Separate local commands/results, environment blockers, requirement status, commit SHA, PR number/head SHA, workflow/check identity, and merge commit. Use only repository status vocabulary and never infer release PASS from percentages.
