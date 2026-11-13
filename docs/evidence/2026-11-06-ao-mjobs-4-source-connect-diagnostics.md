# Source connect phase diagnostics — PR960 / issue770

This continuation adds test-only diagnostics, not a production timing fix.
The original pre-SSL failure was NOT_REPRODUCED locally; root cause remains
unconfirmed. Existing 0.8-second operation, 1-second engagement and 0.35-second
cancellation bounds and assertions remain unchanged.

## Revision and verification

Input PR960: `eb92e4f463f72682c2f9741a09052dde69d69a4a`.
Refreshed main: `6a47f56204462da1aea5ba59ff8a5f88346d05cd`.
Exact tested tree: `6e9d354e71d640f41a3a2df2116f7b0fa2859018`.
The refresh applied without conflicts and preserved the owned dirty diagnostic
blob `f2ed3411afdd1bc44ece2ee0bee55fde61643129`.
This evidence file is added after testing; executable inputs are unchanged.

Windows, Python3.12.10; started2026-11-06T05:56:29UTC:

```text
python -B -m pytest -q -s -p no:cacheprovider --tb=short tests/integration/test_source_adapter_connect_cancellation.py::SourceAdapterConnectCancellationTests::test_finite_worker_deadline_during_real_tcp_connect tests/integration/test_source_adapter_connect_cancellation.py::SourceAdapterConnectCancellationTests::test_worker_phase_diagnostic_excludes_private_results
```

Result: **2 passed,6 subtests passed in14.40s**, no failures or skips.
The finite-connect case exercised Greenhouse, RemoteOK and WWR. The privacy
negative cases supplied synthetic private SQL/path/message strings and malformed
category values; only allowlisted status/error categories were returned.
No other local suite or hosted retry was run for this continuation.

## Diagnostic boundaries

Only the owned connect-test file changes executable behavior. Fixed-name phase
samples observe the owned worker's profile lookup, control reads, adapter factory
and search, capped at16 samples per phase. SSL entry/exit is recorded separately.
Samples retain timestamps, never arguments, SQL, returned values or exceptions.
The search span may extend beyond SSL; SSL timestamps distinguish that interval.
Raw phase/exception dumps are replaced with timestamps, counts and allowlisted
worker result categories. Wrapped production methods still execute unchanged.

## Preserved failures and limits

- PR949 run42168483477/job127730469100: Greenhouse finite-deadline engagement
  failed before SSL/TCP, collector entry0.7ms, DEAD_LETTERED at981.1ms,
  SSL factory0/0, connects0, requests0. The shard reported1failed,845passed,
  11skipped,492passing subtests in742.25s. The previous diagnostic did not retain
  the returned error category; the cause is unconfirmed.
- PR933 run41960418742/job127026534278: Lever global engagement similarly failed
  before SSL/TCP, ending DEAD_LETTERED at927.3ms. Similar phase shape is not proof
  of a shared root cause.
- PR964 run42170776172/job127737825928: cancellation commit elapsed0.934627662s
  against0.35s. The no-premature-reader assertion passed before that assertion;
  the deliberate reader-lock delay was absent. The elapsed interval includes
  fixture patch setup, thread startup, audit and commit, without phase evidence
  isolating the cost. This separate failure remains unresolved; no cancellation
  fix or threshold relaxation is claimed.

Fresh native review and required hosted checks remain mandatory. Existing cleanup
history is preserved. No canonical acceptance, merge, deployment or live-provider
submission is claimed; issue770 remains PARTIAL.
