# Versioning and release engineering

`VERSION` is currently `0.1.0-dev`: an unreleased developer preview, not a claim of 100% acceptance. Use `v0.x.y-alpha.N` for deliberately accepted previews, `v0.x.y-rc.N` for release candidates, and stable tags only after the supported product contract is verified.

## Historic tags are not product versions

The former `archive/mjobs/20261010/*` tags and Git-bundle release are recovery material. Verified copies live in owner-controlled private recovery storage. Retiring public recovery pointers does not erase Git history or recall downloaded copies. Do not republish private archives as release assets.

## Publication gate

Before publication, validate exact-head CI and connected acceptance, unresolved high/critical security findings, full applicable browser regressions, clean source state and the declared acceptance scope. Do not create a release merely to complete a housekeeping task.

The release workflow builds a Docker image from the declared source and can attach provenance/SBOM data on an explicitly authorized trusted release. It must not run publication steps on a public PR. No public image contains `.env`, Git internals, recovery bundles, applicant documents or local outputs.

A failed gate prevents publication; an image build alone is not release acceptance. Until all gates are satisfied, source installation remains the supported distribution and the workflow is useful in validation-only mode.

## Practical procedure

1. Update `CHANGELOG.md` and `VERSION` for a real milestone; open a PR.
2. Run the applicable fast, integration, connected and full-browser suites for that source revision.
3. Resolve security findings or document legitimate reviewed false positives individually; never globally suppress them to release.
4. Merge through required checks. Run release validation on main.
5. Only when validation passes, create the appropriate version tag and publish immutable, digest-identified artifacts.
6. Retain a rollback source/image digest and verify installation without touching existing user volumes.

Dependencies retain their license files in distributions. Apache-2.0 applies to original MJobs work, not external providers' content or third-party packages.
