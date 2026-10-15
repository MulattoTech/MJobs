# Contributing to MJobs

Original MJobs contributions use [Apache-2.0](LICENSE); dependencies and incorporated third-party material retain their own terms. Preserve attribution and use the reference-provenance policy before importing code from another project.

Use existing issues before opening duplicates. Include expected behavior, exact revision and a minimal reproduction with synthetic data. Never post real resumes, answers, tokens, browser sessions or private logs. Report vulnerabilities through [SECURITY.md](SECURITY.md).

For code changes, use a short-lived branch, regression tests and a reviewable PR. Link requirements, report actual checks and limitations, and preserve personal runtime state. The sole integration owner may use native auto-merge after required checks; automation is not human review. Major dependency/security/workflow changes still require explicit maintainer/authorized-agent inspection.

Follow [AGENTS.md](AGENTS.md), [development setup](docs/DEVELOPMENT.md) and [automation policy](docs/maintainers/AUTOMATION.md). Do not close a parent requirement after validating only one slice. No automated test submits a real external application.

Follow the [PR publication policy](docs/PR_PUBLICATION_POLICY.md) before publishing:
default 32 concurrently open development PRs (drafts included), soft warning at
capacity, authenticated dependency automation excluded. Authorship/category use
reviewed metadata; unknowns count. Publication capacity does not grant CI capacity
or integration authority. Meaningful commits, ownership, executed local validation,
nonduplication and no-empty-PR gates apply at every capacity.
