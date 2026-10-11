# Contributing to MJobs

MJobs is public for visibility and testing, but **no source license has been selected yet**. Please review that constraint before copying, redistributing or substantially modifying code.

## Suggest a feature or report a bug
Search existing GitHub issues first. Include a version/commit, observed and expected behavior, reproduction steps with synthetic data, and the relevant P0–P6 priority/category/area when known. Do not post sensitive career profiles, credentials, browser cookies, or real applicant data.

## Propose a code change
1. Discuss major architectural changes in an issue before implementing them.
2. Work from a current main commit on a short-lived branch; preserve local state.
3. Write regression tests for the changed behavior. Do not hide failures or weaken acceptance.
4. Run focused Python tests and/or Vitest/TypeScript build, plus the applicable hosted checks.
5. Submit a PR referencing issue(s), limitations, test results and the exact affected acceptance criteria.

Hosted CI is advisory until baseline suites have been repaired and required checks are configured. Changes to security or application submission controls need explicit review. **Never submit real external job applications as tests.**

The canonical contributor/agent safety contract is [AGENTS.md](AGENTS.md), and the developer quick start is in [README.md](README.md).
