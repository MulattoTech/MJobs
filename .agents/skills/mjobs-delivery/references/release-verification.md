> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

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
