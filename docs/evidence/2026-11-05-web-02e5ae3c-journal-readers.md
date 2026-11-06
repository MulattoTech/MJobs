# MJ-REQ-008: coherent checkpoint readers

Run: `web-20261105T012947Z-02e5ae3c5d644f53a14c166656ca2d82`.
Source baseline: `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1`.
Tested source: `c6cfe36a7217c0afcc97ac97a9c90319d1b50fe7`.
Requirement: [MJ-REQ-008 / issue 9](https://github.com/MulattoTech/MJobs/issues/9).

## Reproduction and behavior

A real fixture publisher was paused after canonical replacement and before
generated-copy replacement. On unchanged baseline source, default and explicit
validation/resume reported a transient mismatch as an invalid checkpoint;
explicit output resume instead returned the older checkpoint successfully.
A separately paused reader also allowed a cooperating writer to complete
between loading its snapshot and validating the journal.

Six new test methods produced seven failing assertions on the baseline
(five reader-mode subcases, reader/writer exclusion, and waiting-reader recovery).
The baseline run exited 1 in 1.182 seconds. Three other new methods already
passed; this is a scoped concurrency defect, not a claim that every read failed.

Canonical validation and resume now acquire the existing bounded OS lock and
validate the same loaded snapshot they return. Explicit canonical sources use
their own coordination file; explicit generated-copy reads wait for the root's
canonical publisher when present. Output-only recovery works without creating
a missing planning directory. Already-locked record/sync paths validate their
loaded state without recursively reacquiring the lock.

Readers return a fixed busy error when acquisition exceeds the existing
three-second limit. A completed publisher permits a consistent retry. A writer
that actually exits between replacements still leaves a detectable mismatch;
explicit sync remains necessary. Neither reader changes journal contents.

## Executed verification

The same command ran against the named source on Linux and native Windows:

```text
python -m unittest -v tests.unit.test_progress_journal tests.unit.test_progress_journal_recovery tests.unit.test_progress_journal_concurrency tests.unit.test_progress_journal_durability tests.unit.test_progress_journal_readers
```

| Environment | Result | Scope |
| --- | --- | --- |
| Linux | 42 PASS, 1 platform SKIP, 0 FAIL; 5.906 s | 43 methods; native non-POSIX compatibility control skipped |
| Native Windows | 36 PASS, 7 platform SKIP, 0 FAIL; 8.921 s | 43 methods; POSIX-only descriptor/symlink cases skipped |

Windows completed at `2026-11-05T01:39:19.8302376Z`, with exact source HEAD
read-back. Its initial PowerShell harness aborted on ordinary unittest stderr
under `ErrorActionPreference=Stop`; that is **HARNESS_ERROR**, not a product test
failure or pass. The completed run used `cmd.exe /d /c` for stdout/stderr file
redirection, preserving the test command and the original failed harness log.

Additional PASS: catalogue validation, progress validation, dashboard freshness,
public repository policy, whitespace and commit provenance (two authored Python
source/test files). No new dependency is needed. Focused Ruff/mypy are NOT RUN
locally because those tools are not installed in the isolated Linux runtime;
existing protected hosted checks remain required. Repeated source runs and the
two platforms are overlapping evidence, not additive feature coverage.

Tests cover five CLI reader forms; bounded refusal with unchanged journal bytes;
waiting for a real publisher; a reader retaining ownership through validation;
invalid-history cleanup; independent journals; and output-only recovery.
Inherited tests retain real process-death recovery, concurrent publishers,
chronology/evidence validation, output consistency, directory flush failures,
symlink/hard-link refusal and explicit repair without duplicate milestones.

## Ownership, limits and release

Atomic claim `refs/heads/webpro-claims/MJ-REQ-008` was created with HTTP 201 and
matching read-back at `2026-11-05T01:32:06.8350370Z`. Owner object:
`42caf22ebc6632a451096ddaf4004c9c76663cdf`. It was revalidated before editing and
publication. Work used an isolated Linux checkout and a separate Windows
validation checkout. No heavy/browser resource slot was needed for these
standard-library focused tests. No shared checkout, live journal or user data
was used as a test fixture; no separate AI/model process was launched.

This changes reader coordination permissions: first use may create the ignored
coordination file, and later reads need access to it. Readers serialize with
each other as well as writers. The lock acquisition wait is bounded; arbitrary
filesystem I/O and flush duration are not newly bounded. Explicit generated-copy
selection still validates the chosen standalone copy, not its freshness against
canonical history. Nonparticipating writers and network filesystems are outside
the guarantee. Physical host/power interruption, full requirement acceptance,
and production deployment are NOT RUN.

Author review is complete; independent review and current-candidate hosted CI
are pending at publication. Protected merge and guarded deployment require
their separate ownership and gates. Rollback is a protected revert; there is no
schema migration or data rewrite. MJ-REQ-008 remains PARTIAL and all acceptance
denominators and earlier evidence remain unchanged.
