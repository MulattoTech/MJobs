# Source TCP cleanup preserves the primary request outcome

Run: `web-20261105T024405Z-5fb5f0e00ed3`.
Requirement: **MJ-REQ-004 / issue #5 remains PARTIAL**.
Original PR960 head: `18a59665fc4ed10bf70bc1c3008ae5c3685f1d8b`.
Tested implementation commit: `3b411bcf1be8f2a63c513c214abdecc53a707749`.
Production source blob: `9b67f712daf745f996455fa3c82415d5439323dd`.

## Reproduced defect and scope

The independent [original TLS review](https://github.com/MulattoTech/MJobs/pull/960#pullrequestreview-5779589134)
identified a separate pre-existing TCP cleanup problem. An expected `OSError` from
closing a discarded socket could replace cancellation/deadline/connect failures,
skip the next address or control check, stop cleanup of other pending sockets, or
override the return of a healthy winning connection.

This continuation adds a private discarded-socket close helper and uses it at
the three TCP disposal sites. Only `OSError` is contained there. Each remaining
pending close is attempted despite an earlier expected close failure. Explicit
`_Stream.close()` still reports its error, and unexpected non-`OSError` cleanup
errors remain visible. The original reviewed `_Stream` source segment, including
its TLS exception-preservation fix, was programmatically checked byte-for-byte
unchanged. Deadlines, clocks, address limits, DNS ownership and production TLS policy were
not modified.

## Failure-first and final verification

| Execution | Observed result |
| --- | --- |
| Two new modules against unchanged original PR960 source, before production edit | Expected RED: pytest reported 11 failed / 3 passed, including four failed stop subcases; no collection error; 2.51 s pytest |
| Same two modules after TCP correction | 10 tests +4 subtests PASS; zero failures/skips; 1.83 s pytest |
| Final source and lint-corrected tests, six adjacent modules | **55 tests +37 subtests PASS; zero failures/errors/skips; 113.92 s pytest /115.844 s command** |
| New test Ruff | PASS |
| Production Ruff | Two existing findings unchanged by code/message/source line: I001 import ordering and B023 in the existing write callback; no new finding or suppression |
| Catalogue, journal, dashboard, whitespace and strict contribution provenance | PASS; dashboard CURRENT |

The final six-module run completed at **2026-11-05T04:09:13.309904Z** on native
Windows, Python 3.12.10. The tests use private temporary data, synthetic controls
and owned loopback fixtures; live providers are disabled. Import/context grouping
and explicit synchronous callback bindings were corrected for lint after the
initial red/green execution; outcome assertions were preserved.

```text
python -B -m pytest -q -p no:cacheprovider -o faulthandler_timeout=60 --tb=short \
  --basetemp <owned-temp> --junitxml <owned-report> \
  tests/unit/test_source_http_tls_cleanup.py \
  tests/integration/test_source_http_tls_cleanup.py \
  tests/integration/test_source_http_socket_budget.py \
  tests/integration/test_source_adapter_connect_cancellation.py \
  tests/unit/test_source_http_tcp_cleanup.py \
  tests/integration/test_source_http_tcp_cleanup.py
```

New unit controls preserve exact cancellation, control-unavailable, timeout and
interrupt exception identity; exercise immediate and asynchronous address failure;
verify the stable connect error/cause and healthy winner; and retain explicit-close
and unexpected-cleanup behavior.

New loopback tests create actual pending sockets. They inject an expected close
error **after the real descriptor is closed**, then assert the production path
closed every discarded descriptor before the fixture's outer safety cleanup.
Cancellation and injected timeout exception identity are retained. A stalled
losing address and a healthy peer prove that the returned winner remains open
and carries an actual HTTP request/200 response. Only the healthy peer receives
the request. Adjacent existing modules exercise actual deadlines, supported
source adapters, DNS-child cleanup, TLS certificate/hostname validation and
worker control behavior.

## TLS security trace and explicit fixture minimum

The original-head [CodeQL check](https://github.com/MulattoTech/MJobs/runs/127029908253)
reported insecure TLSv1/TLSv1.1 originating from `ssl.create_default_context` at
the unchanged wrap-socket boundary. The initial merge-ref analysis query returned
no trace, so the question was retained as NEEDS_REVIEW.

A subsequent query of **`refs/pull/960/head`**, Python analysis **2100406415**,
retrieved the actual SARIF result for **py/insecure-protocol**, alert131. Its
related source is `tests/integration/test_source_http_tls_cleanup.py:15`,
the test-owned default SSL context; its sink is the production
`_Stream.start_tls` wrap call. The result was inspected directly rather than
inferring the source from the annotation alone.

A network-free probe of the default `SourceTransport` on Python3.12.10 /
OpenSSL3.0.16 observed minimum `TLSv1_2`, `check_hostname=True`, and
`verify_mode=CERT_REQUIRED`. The test context already used the corresponding
secure default. Commit **`db2d800253fef1c1e069150fe068cb9a944f9c31`** now states
`context.minimum_version = ssl.TLSVersion.TLSv1_2` explicitly in that owned
loopback fixture, leaving its existing hostname/certificate assertions and
cleanup expectations intact. This makes the fixture protocol floor explicit
to both runtime and static analysis. Production TLS configuration is unchanged.

After this test-only clarification, the two original TLS modules passed
**8 tests +8 subtests, zero failures/skips;0.83s pytest /1.234s command** at
2026-11-05T04:14:58.788526Z. Fixture Ruff and strict provenance passed. The changed
test blob is `3d051a0334ed560f3a37bcf0ac7b64c3ee64c0b7`.

**Fresh hosted CodeQL remains pending.** No alert dismissal, scanner exception,
rule suppression, weakened assertion or claim that every custom SSL context is
safe was introduced. The original failed check and source trace remain evidence.

## Ownership, attribution and limits

Scoped owner `refs/heads/webpro-claims/PR-960-INTEGRATION` points to
`a68d6f77981c8bcea56011b698d9c0dcb327e783`: HTTP201/exact readback at
2026-11-05T03:53:47.947139Z, revalidated before edits and commits. The original
source owner had released its claim. Existing TLS author evidence and independent
reviews remain attributed to their original sessions; reinspection of this
continuation by its author is not an independent TCP review.

This fixes outcome preservation for expected cleanup errors; it does not make
arbitrary socket close calls preemptible, prove a descriptor closes if the real
OS close itself fails, or establish every external-operation criterion. There
are no dependency/schema/user-data migrations. Rollback is a protected revert.

The separate PR949 hosted source-connect and durable AI TLS timing failures are
retained on that PR and issue #770; this patch does not claim to resolve them.
No canonical acceptance, Project/Fleet status, production state or external
application was changed. **IMPLEMENTED / FOCUSED TESTS PASS; NOT MERGED / NOT DEPLOYED.**

## 2026-11-05 follow-up: protected integration failure diagnostics

Current source parent `7cc4809a0168de733da72b60dbd2185dc3b1b7f2` / checked merge `d6224fbda5a733a9d499dd788eff5a47da4a14cf` passed fresh CodeQL check127047721909, CI/required, dependency review, secret scan and disposable Compose. The original CodeQL failure above is retained; the explicit fixture TLS1.2 floor resolved that fresh check without suppression.

The required PostgreSQL/Redis aggregate check127053705493 failed. [Shard2, job127047563435](https://github.com/MulattoTech/MJobs/actions/runs/41967429324/job/127047563435) observed `['BLOCKED','DEAD_LETTERED']` instead of `['BLOCKED','COMPLETED']` in `RouteAttemptConcurrencyTests.test_two_real_workers_only_one_submits_then_explicit_retry_is_safe`. It reports1 failed,997 passed,10 skipped,561 subtests passed,1024.68s; shards0/1 passed. The log did not expose the dead-lettered worker's error category, so no browser, DB or source-transport cause is inferred.

The test-only follow-up attaches each synthetic worker's existing sanitized `status` and `error_category` to that assertion. It preserves the expected status pair, one-attempt/one-submission checks, durable state and explicit retry controls. No production source, retry policy, timing threshold or required check changes. Final diagnostic test blob: `7b8b028db503f54dd5d8a0535de59a8de7a760e1`.

Verification on Python3.12.16 in a resource-admitted, network-disabled Linux container with read-only candidate code and synthetic tmpfs data: **full route-concurrency module5 methods PASS;0 failures/errors/skips**,11.794s. A separate control changed only the returned synthetic worker summary after the real fixture work: it produced exactly one expected assertion failure, showed `SYNTHETIC_DIAGNOSTIC_FAILURE`, and kept an added synthetic private value out of the diagnostic. Normal module plus control:18.139s. Ruff retains the same4 prior code/message findings, without new suppressions.

An earlier unchanged Linux diagnostic passed this race method and the separate durable TLS timing method (+6 TLS subtests) in9.907s. Neither isolated result reproduces or dismisses the full hosted-shard failure. The retained 55-test/37-subtest TCP/TLS product verification applies to byte-identical production and cleanup-test blobs. Fresh actual-candidate CI remains required. New TCP-extension independent review remains pending; MJ-REQ-004 remains PARTIAL.

The final Linux validation completed2026-11-05T04:53:12.384361Z; heavy owner e714223b08e8d98f6abfe94d35bd5446b5da9baf was released04:53:14.048263Z with HTTP204/verified absence. No external employer submission, production fixture, full-suite rerun or browser-profile sharing was used.
