# Remote CI authorization blocker

Result: BLOCKED_EXTERNAL. On 2026-09-29, `git push -u origin
codex/mj-pr-001-governance-safety` was rejected by GitHub because the OAuth
authorization could not create `.github/workflows/ci.yml` without workflow scope.
No remote workflow or CI run was created. This is a GitHub credential-scope
boundary, not a code/test failure.

The complete original local commit `b937b00` remains preserved on
`codex/local-governance-with-ci`. The publishable governance branch retains the
reviewed definition outside the protected path at `infra/ci/verification.yml`.
The mock-spine branch extends it with dependency installation, headless tests,
Compose E2E, outage recovery and fictional evidence retention.

User credential authorization is required to unblock remote verification. The
interactive command is `gh auth refresh -h github.com -s workflow`. After the
user completes that consent, promote the staged definition to
`.github/workflows/verification.yml`, push, and inspect executed Actions evidence.
Do not mark CI PASS or merge dependent implementation solely from local tests.
