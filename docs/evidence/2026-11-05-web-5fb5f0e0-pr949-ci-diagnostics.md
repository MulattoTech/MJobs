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
