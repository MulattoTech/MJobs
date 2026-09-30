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

## Resolution on 2026-09-30

The active GitHub authorization now includes `workflow` scope. The preserved
governance workflow was published on PR #367. The governance, mock spine,
worker lifecycle, and connected UI branches each completed their scoped
GitHub Actions checks. Exact heads, run IDs, commands and retained artifacts
are recorded in `2026-09-30-governance-ci.json`, `2026-09-30-ci-368.json`,
`2026-09-30-ci-369.json` and `2026-09-30-ci-370.json`. Full-spec and release
readiness remain separate from these branch checks.
