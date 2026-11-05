# Recovering an interrupted implementation checkpoint

The canonical journal is `planning/IMPLEMENTATION_STATE.json`. The generated
`outputs/job-platform-implementation-state.json` is a copy of its latest
milestone, including all eleven required checkpoint fields.

Recording publishes these two files with separate atomic replacements. A
process can stop after the canonical replacement and before the generated
copy is replaced. The new milestone is already recorded even if the command
did not report success. Do not blindly append the same milestone again.

On POSIX, each publication flushes the temporary file before replacement and
then flushes the containing directory. Flushing file contents alone does not
persist the changed directory entry. A failed directory open or flush is an
error, including when the filesystem does not support that operation; it is
never silently reported as durable success. The replacement may already be
visible when this error occurs. Inspect and recover it with explicit sync
instead of assuming the old checkpoint is still current.

Explicit sync also flushes the canonical directory before publishing the
generated copy. This repairs the barrier missed by a writer that exited or
failed just after canonical replacement, without rewriting canonical bytes
or appending a duplicate milestone. If that flush fails, sync stops before
changing the generated copy. The journal lock is released on either outcome.

Windows retains its existing file-flush and atomic-replacement behavior; this
change does not establish Windows directory durability. The POSIX barrier
covers entries in the immediate parent directories. Newly created ancestor
directories, filesystem/device persistence guarantees, physical power loss,
and host restart remain separate, unverified durability obligations. Directory
flushes, like the existing file flush, are synchronous and not time-bounded.

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
recover missing canonical evidence or recover checkpoints already lost by older,
uncoordinated writers.
An explicit `--output` writes only that destination; it does not repair a
different default output as a side effect. Both `sync --output` and
`record --output-path` refuse a destination that resolves to the canonical
journal, including normalized aliases. Refusal happens before either journal
is written, so a mistaken output path cannot replace canonical history with
its generated summary.

Strict consistency checks compare every generated authoritative field and the
milestone ID, timestamp and schema version. A matching phase and commit alone
are insufficient when completed work, remaining work or limitations differ.

## Cooperating readers and writers

Record, explicit sync, validation and resume share a local operating-system
lock for the whole read, validation and publication sequence. A competing
command waits for at most three seconds to acquire it, then raises a fixed
busy error before returning a checkpoint or changing the journal.
Once the active publisher finishes, retry the refused
command after inspecting the latest checkpoint. A command interrupted after
canonical publication still needs the explicit sync procedure above.

Readers hold the same lock through validation and construction of the returned
snapshot. They wait for an active two-file publication rather than reporting its
temporary mismatch as corruption. If the writer finishes within the acquisition
budget, the reader validates the completed publication. If it does not, the
reader reports busy without changing either journal. A genuinely stale generated
copy left after a writer exits is still rejected by canonical validation/resume;
readers never silently repair it or ignore the consistency checks.

An explicit canonical `--source` uses that file's coordination lock. Its initial
format inspection may precede acquisition, but the returned snapshot is reloaded
and validated under the lock. Explicit generated-copy reads coordinate with the
root's canonical publisher when that journal exists. They continue to validate
the chosen copy as a standalone checkpoint, not prove that a historical copy is
the latest canonical milestone. Output-only recovery still works when the
canonical journal and planning directory are absent, without creating either.

The coordination file is `planning/.implementation-progress.lock`. It is
created privately beside the canonical file and remains present after release;
its presence alone does not mean a writer is active. Do not delete, replace
or use it as an output destination while publishers may run. Both commands
refuse that destination, including normalized aliases, and refuse symbolic
links or multi-link coordination files. The ignored file contains no journal
data. First use, including validation/resume, needs permission to create it in
the planning directory; subsequent commands need access to the coordination
file. Neither reader changes journal contents.

The lock is released when the operation finishes, fails, or its process exits.
Windows uses a nonblocking byte-range lock; Linux uses an advisory file lock.
The acquisition wait is bounded, not arbitrary filesystem I/O, flush duration
or the full publication/validation operation. Readers serialize with each other
as well as writers, so a busy error may also indicate another active reader.

This coordinates commands using this implementation on the same local journal.
It does not coordinate separate Git worktrees, manual editors, older tools or
network filesystems, and does not replace the GitHub integration ownership
protocol. Those writers must still be serialized by the operator.

## Verification and limits

The regression terminates an owned child process immediately after the real
canonical replacement, then uses fresh CLI processes to detect the stale copy,
sync, resume and record the next milestone without duplicating the committed
one. Further cases cover corrupt generated JSON, invalid canonical data,
missing evidence, a failed replacement with safe retry, custom destinations,
and read-only validation of every checkpoint field. Process-level contention
regressions cover record/record, record/sync, sync/record, bounded refusal and
subsequent retry, coordination-file output aliases, unrelated journals, and
hard-link/symlink refusal. The existing real process-exit regression also checks
that an abandoned OS lock does not prevent fresh-process sync.

Reader process regressions pause a real publication between its two replacements
and exercise default/explicit validation, canonical resume and generated-copy
resume. They cover bounded busy classification without journal writes, retry,
a reader waiting for publication to finish, a reader retaining its snapshot
through validation, output-only recovery, lock release after invalid history,
and independence of unrelated journals.

Durability regressions observe real file/directory flushes in publication
order and inject failures at the content, directory-open and directory-flush
boundaries. They verify descriptor and temporary-file cleanup, propagation
of uncertain-publication errors, canonical-directory repair before derived
publication, and fresh-process recovery without a duplicate milestone. These
are syscall-order and failure-path checks, not simulated proof of power loss.

This proves recovery at the two-file process-interruption boundary. It does
not establish physical host-restart/power-loss durability, recovery of already
lost checkpoints, coordination with nonparticipating writers, or the complete
MJ-REQ-008 requirement. Those remain separate acceptance obligations.
