# Recovering an interrupted implementation checkpoint

The canonical journal is `planning/IMPLEMENTATION_STATE.json`. The generated
`outputs/job-platform-implementation-state.json` is a copy of its latest
milestone, including all eleven required checkpoint fields.

Recording publishes these two files with separate atomic replacements. A
process can stop after the canonical replacement and before the generated
copy is replaced. The new milestone is already recorded even if the command
did not report success. Do not blindly append the same milestone again.

## Explicit recovery

From the intended checkout, with its normal single-writer ownership:

1. Run `python -m tools.verification.progress validate`. A stale or corrupt
   generated copy is reported; validation and resume remain read-only.
2. Run `python -m tools.verification.progress sync`. This validates canonical
   history, evidence paths, chronology, phase and last checkpoint independently
   of the generated copy, then atomically rebuilds that copy.
3. Run `python -m tools.verification.progress validate`, then
   `python -m tools.verification.progress resume`. Inspect the latest milestone
   before recording any further work.

Sync does not append, remove or rewrite canonical milestones. It refuses an
invalid canonical journal even when the generated copy is valid. It cannot
recover missing canonical evidence or decide between competing writers.
An explicit `--output` writes only that destination; it does not repair a
different default output as a side effect.

Strict consistency checks compare every generated authoritative field and the
milestone ID, timestamp and schema version. A matching phase and commit alone
are insufficient when completed work, remaining work or limitations differ.

## Verification and limits

The regression terminates an owned child process immediately after the real
canonical replacement, then uses fresh CLI processes to detect the stale copy,
sync, resume and record the next milestone without duplicating the committed
one. Further cases cover corrupt generated JSON, invalid canonical data,
missing evidence, a failed replacement with safe retry, custom destinations,
and read-only validation of every checkpoint field.

This proves recovery at the two-file process-interruption boundary. It does
not establish physical host-restart/power-loss durability, concurrent-writer
coordination or the complete MJ-REQ-008 requirement. Those remain separate
acceptance obligations.
