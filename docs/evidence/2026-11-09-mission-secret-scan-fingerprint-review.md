# PR #1013: reviewed historical Git-object secret-scan matches

The GitHub "Secret scan - reviewed history" run `42697451654` reported five
`generic-api-key` candidates in unrelated, already-published MJobs evidence
commits. They were not introduced by the Mission Control activity change.

Each reported line was inspected without publishing the suspected value:

- One `MJ-REQ-205` evidence field maps a test-file path to its Git SHA-1 blob.
- Three `MJ-REQ-111` evidence fields map an integration-test path to its Git
  SHA-1 blob, across two historical revisions.
- One sensitive-answer evidence field records an exact Git commit SHA-1.

For all five candidates, the 40-hex value was independently checked against
the corresponding GitHub Contents blob identity (or the Git commit endpoint).
Every comparison matched. These are repository object references, not
credentials; no secret was rotated, logged, copied, or inferred.

The accompanying `.gitleaksignore` additions match **only** the five reported,
immutable `commit:path:rule:line` fingerprints. No scanner rule, directory,
branch, or general key format was excluded. Future unrelated secret findings
remain actionable. This review covers only the five reported matches, not an
exhaustive repository security assessment.

Policy: `SECURITY.md` requires source-control credential exposures to be
handled through provider revocation/rotation before history cleanup; no such
credential exposure was established for these five findings.
