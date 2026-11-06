# PR949 integration timing diagnostics â€” 2026-11-05

Run: `web-20261105T024405Z-5fb5f0e00ed3`. Scope: MJ-REQ-004 diagnostic continuation of the owned PR949 recovery candidate. **Diagnostic instrumentation only; root cause unconfirmed; acceptance remains PARTIAL.**

## Retained hosted failures

The actual merge candidate `fffa2b05662987ebcde924b77a7051d4d7ddacb7` (head `65317ca499ff2adb0a10b84ae346560555e89785`, main `ecd987dcaecce05eed13391a765f27cf5b9def76`) failed [shard0, job127047537700](https://github.com/MulattoTech/MJobs/actions/runs/41967421121/job/127047537700). `DurableTLSRecoveryTests.test_tls_cancellation_and_deadline_persist_without_uncertain_replay`, Azure OpenAI deadline subcase, observed worker terminal 909.6ms against the unchanged 900ms assertion; the peer closed at293.8ms. Result: 1 failed,914 passed,17 skipped,555 subtests passed,810.84s. The required aggregate failed; shards1/2 passed.

The same unchanged test previously observed1574.8ms terminal /294.2ms peer closure on [e5bc5a5 shard0](https://github.com/MulattoTech/MJobs/actions/runs/41963154783/job/127033986900). The older missing Greenhouse connect-entry event remains a separate observation in [issue770](https://github.com/MulattoTech/MJobs/issues/770#issuecomment-6649180835). No common mechanism is established.

## Diagnostic change

The test records the test-owned worker's thread identifier and its exit timestamp, then samples at a nominal750ms only if its future remains pending. It records the actual probe offset and at most12 frame locations, using only file basenames, line numbers and function names. It never prints local values, frame source, full filesystem paths, request payloads or profiles. A pending stack is printed before the original bounded result wait completes so a later timeout can retain the observation.

The existing terminal-observation timestamp and900ms assertion remain unchanged. The extra worker-exit timestamp can distinguish time spent inside the worker from a late test-thread observation; neither cause is presumed. The original total three-second result-wait budget is preserved with an absolute deadline. Provider budgets, cancellation-commit assertions, descriptor/client/watcher cleanup, durable reopen checks and terminal redelivery assertions remain intact. No production source or CI policy changes.

Final diagnostic test blob: `ca6a79fe088027635f05b42c37ca93ae7ef86608`. All15 original recovery/discovery/resume source/test/evidence blobs in PR949 remain exact.

## Executed verification

- Unchanged Linux control at PR960 head7cc4809, with the identical original TLS-test blob79d33dd8: both previously failing methods passed, plus6 TLS subtests;9.907s. Azure deadline peer295.2ms /terminal298.2ms. This used Python3.12.16, isolated loopback, immutable deployed runtime dependencies, read-only candidate code and synthetic tmpfs storage. It does not reproduce the full hosted pytest shard or its disk/scheduling conditions.
- Intermediate stack-diagnostic blob27622040: full Linux TLS module **3 methods +6 subtests PASS**,6.127s. A deliberately delayed worker still failed the900ms assertion, emitted its sampled function location and excluded a synthetic sensitive local value. The adjacent route module passed5 methods, with its own expected-failure privacy control.
- Final blobca6a79fe: native Python3.12.10 full TLS module **3 methods +6 subtests PASS;0 failures/errors/skips**,8.077s; a deliberate700ms post-worker delay produced exactly one expected subtest failure, exposed the diagnostic and did not expose the synthetic local value. Full normal-plus-negative-control command:16.234s, completed2026-11-05T04:55:48.890908+00:00.
- Ruff: the original10 code/message findings remain unchanged; no new finding or suppression. Two initial closure-capture findings were removed by passing the per-subcase state explicitly.
- No production test, threshold, skip, failure or gate was removed. Historical hosted failures are still blocking until fresh actual-candidate protected checks pass.

Native command: `python -B <owned-diagnostic-case.py> tls`, loading `DurableTLSRecoveryTests` with unittest and a separately scoped negative-control patch. Normal tests use the repository's unchanged assertions; the injected delay exists only in the private verification harness. Private logs retain commands, source blobs, durations and the control assertions.

The source claim and each heavy slot were obtained through HTTP201/exact readback. Both Linux diagnostic slots were exact-owner released (c1934643 at04:48:24Z; e714223b at04:53:14Z). Publication uses the separate short integration lock; no heavy slot is held while hosted CI runs.

**Not a fix claim:** isolated success does not dismiss either hosted failure. Authenticated browser acceptance, full MJ-REQ-004 acceptance and production behavior from this open PR are not established.

## AO GC phase instrumentation continuation — 2026-11-06

Owner: `mjobs-2`; sole integration coordinator: `mjobs-1`. This is **instrumentation,
not a fix**. The ordinary refresh includes main
`2621132e0a37c64d4b2a2b04960c91fc0cb49733` while preserving the original PR949,
PR941 and PR932 histories. No authored product, shared persistence, operation,
scanner-policy or canonical acceptance changes.

The new exact-head failure on `11128e62158fdffa8548c909b754a63eba8997a4` is retained:
[run42165010203, shard0/job127719326501](https://github.com/MulattoTech/MJobs/actions/runs/42165010203/job/127719326501)
observed Azure deadline peer closure at293.1ms and worker exit/terminal at905.1ms
against900ms. At807.305ms the sampled stack was in SQLAlchemy
`registry.py:104:_collection_gced`, through result setup for the terminal-state
transaction SELECT at `tasks.py:566`. Result:1 failed,938 passed,17 skipped,
649 passing subtests,1096.06s. The preceding failure sampled the same callback
with998.5ms terminal/291.8ms peer closure. Neither sample measures the complete
GC interval or proves that GC accounts for the delay.

### Bounded baseline observation

Before source edits, one unchanged failing-method run per revision used a
temporary value-free GC callback observer, with no forced collection or policy
change. Windows Python3.12.10, SQLAlchemy2.1.1, HTTPX0.28.1, HTTPCore1.0.9;
automatic GC enabled, thresholds `(700, 10, 10)`:

- PR949 `11128e62`:1 test and6 subtests passed,7.69s, UTC05:17:01–05:17:12;
  CPU59%→83%; Azure deadline313ms. Generation2 observations of at least25ms
  took47–203ms on main/API-portal threads, not the worker.
- Main `2621132e`:1 test and6 subtests passed,7.39s, UTC05:17:24–05:17:34;
  CPU27%→65%; Azure deadline328ms. Generation2 observations took31–235ms,
  again on main/API-portal threads, not the worker.

The task and AI transport blobs were identical across these revisions. These
isolated controls did not discriminate the hosted failure; repetition stopped.
The existing setup-collection pattern in another test remains a hypothesis,
not evidence authorizing its use here. This change does not call `gc.collect()`.

### New observation and verification

Test blob: `77de5b28bd746416200f6ba43051f1e19cdded05`.
Tested pre-evidence tree: `180a2d5b386cdc6ff85550e89a97517433375cdd`.

The callback observes the existing executor scope, retaining only four events
per generation (at most12 total) and omitted-event counts. Each event contains
generation, owned-worker/other-thread classification, start/stop offsets from
the existing TLS boundary, and duration. An unfinished event has null stop and
duration. Generation0 churn cannot evict all older-generation observations.
No object values, callback info dictionaries, thread identifiers, locals,
credentials or paths are retained or emitted. The callback is removed on every
context exit. GC remains enabled and unchanged. The original clock, provider
budgets, three-second result-wait budget,900ms assertion, cancellation-commit,
cleanup, durable reopen and redelivery checks are unchanged.

One bounded verification batch ran with the same pinned Windows dependencies,
UTC2026-11-06T05:22:59Z–05:23:15Z, CPU36%→47%:

- Normal complete TLS module:3 tests with all6 provider/cancellation subcases
  passed,8.847s, zero failures/errors/skips. Azure deadline328ms; cancellation219ms.
- Deterministic observer controls passed: exact12-event bound, per-generation
  omission counts, start/stop/duration, both thread classes, unfinished phase,
  callback removal on exceptional exit, privacy and unchanged GC policy.
  These use synthetic callback events and do not establish real GC causality.
- The existing delayed-worker/privacy control pattern injected700ms after the
  first worker's real operation. It produced exactly one expected900ms assertion
  failure, retained the sampled delay and GC fields, and excluded a synthetic
  sensitive local and private path from diagnostic lines. Callback registration
  and GC policy were unchanged afterward. The injected control is temporary,
  not part of the committed test or a passing production acceptance claim.
- Ruff comparison found the same10 code/message findings as the prior test blob;
  no new finding or suppression. Whitespace checks passed.

Command: `python <owned-gc-verification.py> <isolated-checkout>`, using unittest
for the normal module and the single expected-failure control. Verification
harness SHA256: `c1cbfab3a40fcaf36b8cc6429b14b7b5b651a7b9bec9e84e8ffaaaba8530e387`.
Raw local logs remain temporary working material. Windows monotonic resolution
can report0ms for short collections; this is not a claim of zero collection work.

Fresh hosted measurement and native review are pending. A GC interval overlapping
a late worker is the next falsifiable observation, not proof by itself. If the
next hosted measurement gives no discrimination, stop rather than loop. No
workflow rerun/dispatch, requirement promotion, merge or deployment is authorized
by this receipt.
