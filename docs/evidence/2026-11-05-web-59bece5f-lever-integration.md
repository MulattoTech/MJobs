# PR954: reviewed Lever integration continuation

Run: `web-20261105T024508Z-59bece5f637e`.
Scope: [PR954](https://github.com/MulattoTech/MJobs/pull/954),
[MJ-REQ-018 / #19](https://github.com/MulattoTech/MJobs/issues/19),
[MJ-REQ-155 / #156](https://github.com/MulattoTech/MJobs/issues/156).

## Reviewed behavior and attribution

The original author corrected optional salary handling that could discard a
complete Lever employer inventory on an oversized integer or non-string
interval. Valid neighboring jobs, raw source snapshots, description fallback,
unknown units and idempotent recollection are covered by the author's source
tests. Original reproduction failures and source evidence remain in
`2026-11-05-web-f867bf6e-lever-salary.md`.

This continuation preserves the original author and two independent technical
reviews. It does not claim original authorship of the Lever fix or add those
reviewers' tests to its own execution count. Independent reviews:
[5778806236](https://github.com/MulattoTech/MJobs/pull/954#pullrequestreview-5778806236)
and [5778829802](https://github.com/MulattoTech/MJobs/pull/954#pullrequestreview-5778829802).

## Exact candidate and executed results

- Previous published head: `e64ef0f8cad6a3d6842aa0757f078c7b3a167ed7`.
- Refreshed protected-main base: `d6650e4bc9bfd784bd6e3a346c793c372f02518a`.
- Tested merge commit: `8e83629387be86d876751afd9e86399c1c730552`.
- Tested tree: `813ed6d180293ebab9e0ddba447546808fca0a16`.
- All three Lever production/test blobs are identical to the previous published
  head. Incoming main touches child-requirement checkpoint tooling and evidence,
  with no overlap with these Lever paths. Twenty other open PR path inventories
  were inspected; none touched these three files.

Executed on native Windows, Python 3.12, with a private virtual environment
reusing installed dependencies read-only. The imported Lever module was asserted
to reside in the isolated checkout. Local httpx was 0.28.1 and SQLAlchemy 2.1.8;
protected hosted CI must still verify the repository's pinned environment.

```text
python -B -m pytest -q -p no:cacheprovider --basetemp <private-temp> --junitxml <private-receipt> tests/unit/test_lever_salary_safety.py tests/unit/test_lever_source.py tests/integration/test_lever_autodiscovery.py --tb=short
```

**PASS: 29 tests and 56 subtests, zero failures/errors/skips**, 15.28 seconds
(pytest summary), exit 0. JUnit records 85 executed cases including the subtests;
these are not 85 independent test methods. The connected fixtures use synthetic
SQLite and mocked public GET responses. No live provider collection was run.

Additional executed PASS at the tested merge: catalogue, progress validation,
dashboard freshness, merge-commit provenance and whitespace. A following
evidence-only commit leaves all three tested source/test blobs unchanged.

## Preserved invocation problems

An initial environment inventory requested optional PostgreSQL driver metadata,
which was absent locally; that inventory stopped before any test execution.
The local scope uses SQLite. Local PostgreSQL/Redis execution remains NOT RUN.

The first publication preflight rejected the operator's evidence format:
`local_validation` was supplied as structured objects, while the existing
validator requires strings. This was an orchestration-data error, not a failed
Lever test or approval override. No push occurred from that attempt. Its log and
candidate remain preserved; the next publication must use corrected evidence
strings and pass the unchanged validator. Do not substitute an inventory-only
preflight for the existing-PR update gate.

## Ownership and delivery limits

Scope claim `refs/heads/webpro-claims/PR-954-INTEGRATION`:
`b07d2c818b663b0f27b4b91cbb1246a5a2ad4616`, HTTP201 and matching readback
at 2026-11-05T02:47:57.7584173Z. Ownership was checked before isolation and edits.
The unique continuation branch is
`web/web-20261105T024508Z-59bece5f637e/pr954-integration`.

The first short integration transaction owned
`1299a40194777be668675f45783cb026c8cf8325` from 02:51:01Z to 02:51:23Z;
owner-only release returned HTTP204 and exact-ref absence. No lock was held
while preparing this evidence. Each subsequent publication/merge requires fresh
integration ownership and current main/head readback.

At this evidence checkpoint: **locally merged and focused-tested; fresh hosted
checks, protected merge and guarded deployment are still pending**. No canonical
acceptance or denominator changes; MJ-REQ-018 and MJ-REQ-155 remain PARTIAL.
No GitHub Project/Fleet acceptance update, external application, separate AI
agent, paid model call, shared dependency mutation, heavy/browser slot or
production write was performed.

Rollback is a protected source revert, assessed against intervening changes;
there is no schema migration or user-data rewrite. Final candidate/CI/merge,
release and owner-only claim-release receipts belong in the PR's durable
conversation after the corresponding actions actually occur.

## AO current-main continuation — 2026-11-06

Exclusive continuation owner: `mjobs-3`, assigned under issue #18 / MJ-REQ-017
for this bounded existing Lever robustness slice. The original issue #19/#156
attribution above is preserved; no umbrella requirement is promoted.

Verified published input: `c7e502fabe666b301f7378f6780d7106d28111c2`.
Refreshed main: `560b97eda0fcb0b80f4b63e8fe5b28e805414580`.
Tested candidate tree before this receipt-only append:
`c90671e459d502a08aa8c8b5ab7cfcdc20df2fa0`.
The containing commit names the final candidate and preserves both parents.

The existing fix and regression coverage were reused without product or test
changes. All three owned Lever source/test blobs match the input head exactly;
all 28 incoming main blobs, including its canonical handoff, match main exactly.
Both former objective refs were absent. PR934 remains frozen on its separate
branch and was neither modified nor polled by this continuation.

Executed from the registered AO worktree on Windows/Python 3.12.10 using the
existing pinned environment, with the candidate root on `PYTHONPATH`:

```text
python -X utf8 -B -m pytest -q -p no:cacheprovider tests/unit/test_lever_salary_safety.py tests/unit/test_lever_source.py tests/integration/test_lever_autodiscovery.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning
```

**29 tests and 56 subtests PASS**, zero failures/skips, 30.84 seconds, completed
2026-11-06 around 05:02 UTC. Staged whitespace validation passed. The bounded
fixture batch used authorized heavy slot 3 and has exited; no containers were
started. Prior red tests and independent reviews above remain historical,
separately attributed evidence. No additional tests were invented or rerun.

New-head hosted checks and native review remain pending, as do protected
integration and deployment. PostgreSQL-specific, live-provider, browser and
production validation remain NOT_RUN locally. No canonical acceptance matrices
or new canonical handoff text were authored. No source HTTP, company feeds,
host configuration, external application or production data was changed.
