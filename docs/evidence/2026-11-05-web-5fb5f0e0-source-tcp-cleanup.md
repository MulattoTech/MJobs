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
unchanged. Deadlines, clocks, address limits, DNS ownership and TLS policy were
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

## Retained CodeQL question

The original-head [CodeQL check](https://github.com/MulattoTech/MJobs/runs/127029908253)
reports insecure TLSv1/TLSv1.1 originating from `ssl.create_default_context` at
the unchanged wrap-socket boundary. This continuation does **not** dismiss or
suppress that finding.

A direct, network-free probe of this source's default `SourceTransport` on
Python 3.12.10 / OpenSSL 3.0.16 observed minimum `TLSv1_2`,
`check_hostname=True`, and `verify_mode=CERT_REQUIRED`. This is concrete native
counterevidence, not proof about every caller/context or the hosted analysis.
The full relevant source-to-sink trace was not available in the PR-merge analysis
query. **Disposition remains NEEDS_REVIEW.** Fresh CodeQL and all protected checks
are required on the published current-main candidate. No security gate or rule
was altered.

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
