# One historical Docker image digest disposition

- Run: `web-20261105T012848Z-5c9a70d51d99`.
- Scope: MJ-REQ-175 / issue #176, release-maintenance evidence only.
- Base: `af227103386e4363b3dec3c4453192035699718c`.
- Claim: `refs/heads/webpro-claims/SECRET-SCAN-PUBLIC-IMAGE-DIGEST` at
  `bec21aac8ae7522fe552799ce5236c921514d752`; HTTP201 and exact readback at
  `2026-11-05T03:44:17.836458+00:00`.
- Canonical acceptance is unchanged. MJ-REQ-175 was already PASS; this narrow
  scanner disposition creates no new acceptance credit.

## Preserved failure and provenance

The new main revision's [secret-scan job failed](https://github.com/MulattoTech/MJobs/actions/runs/41962360292/job/127031447572).
Its redacted artifact `13386525102` contains exactly one finding:

```text
0bfeeb458eb32b7de3695dd754a09bcd6304126e:planning/SESSION_HANDOFF.md:generic-api-key:440
```

The [deployment-candidate job](https://github.com/MulattoTech/MJobs/actions/runs/41962360249/job/127031447317)
correctly stopped at its independent-check gate before image validation. No new
updater invocation was made. The preceding production revision remained healthy.

The historical line belongs to another session's published coordination handoff.
The unchanged scanner uses `--log-opts=--all`, so that branch is visible even
though it is not part of the new main's ancestry. The branch and failure evidence
are preserved. No history was rewritten or removed.

Read-only verification at `2026-11-05T03:44:16.709708+00:00` established:

1. The line's sole 64-character hexadecimal value exactly matches the retained
   Docker image object's `.Id` returned by `docker image inspect`.
2. That image is the same immutable object recorded by this run's earlier
   [verified d665 deployment receipt](https://github.com/MulattoTech/MJobs/pull/953#issuecomment-6648539471).
3. Both 40-character values in the line resolve to the public Git commit
   `d6650e4bc9bfd784bd6e3a346c793c372f02518a`; they are revision/tag references.

Retained Docker object:

```text
sha256:6dfe7f51530c9ae080d83e97ec14f41d1fb895a1382d90cb3218a692489fb233
```

The flagged value is an image identifier, not an authentication credential.
Only the exact immutable commit/path/rule/line fingerprint above is added to
`.gitleaksignore`, following its existing reviewed-history convention. Scanner
rules, executable pin, all-ref coverage, redaction, CI checks and updater admission
remain unchanged. There is no rule, directory, value-wide or wildcard exception.

## Executed verification

Execution used Linux x64 Gitleaks **8.30.1**, extracted from the repository's pinned
release archive after independently checking its SHA-256:

```text
551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
```

The following results were recorded at `2026-11-05T03:47:09.936336+00:00`.
Child stdout/stderr were confined; reports retained only scanner-redacted results.

| Execution | Actual result |
| --- | --- |
| Scan the introducing historical commit with the unchanged main ignore file | Expected RED: exit 1, exactly the one fingerprint above |
| Scan the same immutable commit with the proposed ignore file | GREEN: exit 0, zero findings |
| Scan the identical historical line at the same path and line number in a new disposable Git commit using the proposed ignore file | Expected detection: exit 1, one finding bound to the new commit |
| Existing exact-fingerprint and scanner-output/archive guard tests | 6 PASS, zero failures/skips, 0.23 seconds |

The new-commit control used a disposable local repository and the already public
image text; it did not introduce a credential. Its detected commit was
`6e37a40631703517632a9e3b6298257252ca54d1`. The disposable repository was removed.
This proves the new exception does not suppress the same text in another commit.

Reproduction of the two historical scans, from a repository containing the
immutable finding commit, uses the checksum-verified binary and either the base
or proposed `.gitleaksignore`:

```text
gitleaks git . --log-opts=0bfeeb458eb32b7de3695dd754a09bcd6304126e^..0bfeeb458eb32b7de3695dd754a09bcd6304126e --redact --no-banner --report-format json --report-path <private-redacted-report> --gitleaks-ignore-path <base-or-proposed-ignore-file>
python -B -m pytest -q -p no:cacheprovider tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py
```

No product runtime, migration, browser, employer interaction, Fleet control or
acceptance matrix was modified. The 22-open-PR overlap inventory found only an
already merged, byte-identical ignore blob carried in PR929; its integration owner
had explicitly frozen that disposition. This patch does not alter that history.

## Review and release boundary

The reproduction and Docker-object comparison above are the author's evidence.
An independent session still needs to review this exact disposition and proof.
Protected checks must pass on the actual current-main candidate before merge.
The resulting main revision then needs its own release checks and guarded updater
admission. No merged, deployed, whole-requirement or full-repository PASS is
inferred from the focused historical scan.
