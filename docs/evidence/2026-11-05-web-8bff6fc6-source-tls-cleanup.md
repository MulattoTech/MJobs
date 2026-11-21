# Source HTTP TLS setup cleanup preserves the original failure

- Run: `web-20261105T024512Z-8bff6fc6-0a03-44d0-b6f2-3c98498e9165`.
- Requirement: MJ-REQ-004 / issue #5, still PARTIAL.
- Base: `c6d499f391a8e092e06892d7fdea25cbfd177e53`.
- Exact tested source: **743390922d688867344a603f61ea88d1aede6135**.
- Scope: source HTTP transport TLS setup exception cleanup, two dedicated regression modules, this evidence and a scoped handoff.

## Reproduced behavior

`services/discovery/sources/http_transport.py::_Stream.start_tls` caught a primary TLS/control failure, then called `self.close()` before re-raising. An expected socket-close `OSError` replaced the primary exception. A direct unchanged-source reproduction returned OSError instead of the supplied OperationCancelled object.

The complete new regression scope was then executed against unchanged production source at the base above. Pytest reported **11 failing assertions and 5 passes**, including eight failed subcases. The failures cover cancellation, unavailable operation control, a connection deadline, KeyboardInterrupt, SSL wrapping, nonblocking setup and handshake deadline, plus cancellation/deadline on actual raw and wrapped loopback sockets. These are failure assertions/subcases, not eleven separate parent test methods.

Baseline command:

`python -B -m pytest -q -p no:cacheprovider -o faulthandler_timeout=30 tests/unit/test_source_http_tls_cleanup.py tests/integration/test_source_http_tls_cleanup.py --tb=short --junitxml=<owned-output>/baseline.xml`

Baseline exit 1, pytest 1.58s, subprocess 2.015s, finished 2026-11-05T03:21:03.964349+00:00. The source diff was asserted empty before the reproduction; final regression files were already present. Receipts: `baseline.json`, `baseline.log`, `baseline.xml` under the owned ignored output directory.

## Correction and boundaries

The exception cleanup now catches only expected OSError from the socket close and re-raises the original failure. Original exception identity/classification is retained. The real socket close is still attempted exactly once; successful TLS setup keeps its stream until normal explicit close.

Unexpected non-OSError cleanup failures retain their existing visible behavior. This narrow change does not make close calls preemptible, prove every possible socket failure is recoverable, alter deadlines/retries, or establish whole MJ-REQ-004 acceptance. No production incident is asserted from the synthetic reproduction.

PR #957 changes a separate PublicCompanyClient response-cleanup boundary. This patch does not edit that client or its tests. Fresh changed-file inventories of all 22 then-open PRs showed no path overlap before acquisition.

## Executed corrected-source verification

At exact source **743390922d688867344a603f61ea88d1aede6135**:

```text
python -B -m pytest -q -p no:cacheprovider -o faulthandler_timeout=60 \
  tests/unit/test_source_http_tls_cleanup.py \
  tests/integration/test_source_http_tls_cleanup.py \
  tests/integration/test_source_http_socket_budget.py \
  tests/integration/test_source_adapter_connect_cancellation.py \
  --tb=short --junitxml=<owned-output>/focused.xml
```

**45 tests and 33 subtests PASS; zero failures/errors/skips**, pytest 116.83s, subprocess 119.406s, exit 0; finished 2026-11-05T03:24:16.513597+00:00. The process was bounded to 240 seconds, imported the owned source path, used isolated temporary outputs and the pinned environment read-only, and left the tracked worktree clean.

The new loopback tests exercise actual raw sockets and SSLSocket wrappers. They perform the real descriptor close, inject the expected OS-level close error, verify the original cancellation/deadline object, and assert descriptor=-1 plus peer EOF before outer cleanup. These stops occur before ClientHello; completed TLS/certificate/hostname behavior is covered by the adjacent existing suite, not inferred from those stop fixtures.

The adjacent suite also exercises healthy source collection and fresh readback, real TCP/TLS stalls, finite worker deadlines, public cancellation, DNS child disposal, source trust_env policy, alternate-address handling, cold SSL setup accounting and supported adapter transport paths. All network fixtures are synthetic/owned; no public employer request, real application or paid model call was made.

Additional checks:

| Check | Observed result |
| --- | --- |
| New-test Ruff | PASS |
| Production Ruff | Two findings, identical to baseline by code/message/source line; no new finding or suppression |
| Catalogue / progress journal | PASS |
| Dashboard freshness | CURRENT |
| Strict source-commit provenance | PASS for all three source/test files |
| Whitespace / author source review | PASS |
| Independent technical review | NOT_RUN at publication; author review is not independent |
| Fresh protected candidate CI / merge / deployment | PENDING / NOT_RUN |

Source lint and test receipts are retained in `outputs/web-8bff6fc6-source-tls/`: `focused.json`, `focused.log`, `focused.xml`, `ruff-source-baseline.json`, `ruff-source-current.json`, `ruff-tests.log`. Initial context bootstrap supplied an invalid verbose task ID; it was corrected to the stable SOURCE-HTTP-TLS-CLEANUP key, which returned EXECUTE with no checkpoint error. That setup correction changed no product file.

## Ownership and delivery

Claim: `refs/heads/webpro-claims/SOURCE-HTTP-TLS-CLEANUP`, owner **86cf23a8c113a6c4e52dd64946390c9b16d25525**. Atomic HTTP 201 plus exact readback at 2026-11-05T03:18:43.3420020Z; exact owner revalidated before the production edit. Branch: `web/web-20261105T024512Z-8bff6fc6-0a03-44d0-b6f2-3c98498e9165/source-http-tls-cleanup`. The no-op claim commit never enters product history.

No heavy/browser slot or global integration/deployment lock was used for this scoped source/test work. Separate PR #933 PostgreSQL work used and released its own heavy slot earlier in this run; its tests are not counted here.

Rollback is a protected revert; no dependency, schema, migration, stored-user-data or Cloudflare policy changed. Require independent review, all current-candidate protected checks, fresh exclusive integration ownership and separately guarded deployment. Acceptance denominators, canonical status, issue closure, Project and Fleet completion remain unchanged.
