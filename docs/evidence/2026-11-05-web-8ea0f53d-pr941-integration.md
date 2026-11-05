# PR941 current-main integration verification

Run: `web-20261105T012958Z-8ea0f53d51f6`  
Requirement: MJ-REQ-155 and MJ-REQ-004 (both remain PARTIAL)  
PR: https://github.com/MulattoTech/MJobs/pull/941

## Scope and ownership

This run continued the existing discovery rollback PR. It did not author the
original rollback implementation, change its acceptance scope, or deploy it.
The objective claim was acquired with HTTP 201 and exact readback:
`refs/heads/webpro-claims/PR-941-INTEGRATION` at
`7b0889edd94b9c44fefc7f26416f0f7ad57da210`.

The original reviewed implementation and dedicated regression file remained
byte-identical through both conflict-free merges of protected main. The scanner
policy matched protected main after each merge. No gate or failure was waived.

## Exact revision evidence

| Revision | Base | Focused result |
| --- | --- | --- |
| `6cb8116d3983a1ddefed4b7d3a54e6a6bc7c6132` | `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1` | 12 PASS, 0 FAIL/SKIP; 26.99 s |
| `2da457f42e2380718e9f117120f666d75916ec88` | `0d3fbc5749fba8c682033a343a7ec9695e4dd743` | 12 PASS, 0 FAIL/ERROR/SKIP; 13.19 s |

These are repeated executions of the same 12 tests, not 24 unique tests.
The second base includes protected PR937's preview-error privacy fix.
The final publication adds only this evidence and a task handoff to the second
verified source; hosted required checks must run on the final published head.

Command from the isolated worktree:

```text
python -m pytest -q -p no:cacheprovider --basetemp .tmp/pr941/refreshed-tests tests/integration/test_mock_discovery_transaction_control.py tests/unit/test_mock_discovery_client.py --tb=short --junitxml=.tmp/pr941/refreshed.xml
```

The remote process session for the second execution was no longer available
when polled. Its complete pytest log and JUnit report were read back: 12 tests,
0 failures, 0 errors, 0 skips, 13.196 seconds; the unchanged tested HEAD was also
verified. No second execution was launched to replace that lost session receipt.

Existing non-author review with executed tests:
https://github.com/MulattoTech/MJobs/pull/941#pullrequestreview-5777932158

The initial refreshed candidate's GitHub merge commit
`c262e4913ff4d5164106c03c244eb9342a0e10bb` had the same tree as its locally
tested head. CI / required, synthetic Compose, dependency review and secret scan
passed on that head, but main then advanced while PostgreSQL/Redis integration
was still running. Those checks are retained as history, not reused to certify
the second base. Fresh gates and exact final candidate identity remain mandatory.

## Locks, integration and limits

The first short integration lock `c1faa44b2b885d59a53dbe97b962ec3f8d1633a1`
was released with HTTP 204 after normal publication. The second bounded refresh
used `fde3fd066648e7cf42282871d599812f85ecdebb`, acquired with HTTP 201 plus
readback; its release and final branch SHA are recorded in the PR conversation.
Only the run's exact owned refs may be released. No heavy or browser slot was used.

At this evidence checkpoint, the PR is OPEN / NOT MERGED / NOT DEPLOYED.
The broader operation still uses cooperative checkpoints; this work does not
establish hard preemption or public concurrent-SQLite cancellation acceptance.
Canonical matrices, denominators, Project state and Fleet controls are unchanged.

Next: inspect live ownership and current main/head, verify all five required
checks and resolved review threads, then use a fresh short integration lock for
the protected merge. Deployment requires its separate lock and guarded updater.
