# Later evidence for a parked company-discovery request

Discovery intents coalesce by company and source. A request can initially lack
usable employer-origin evidence, then receive a later eligible native posting
from the same source. A parked request may now use that later evidence instead
of remaining permanently tied to the first posting.

The refresh requires all of the following:

- State is `NEEDS_ORIGIN_REVIEW`, with no operation, selected website,
  authorization or domain evidence.
- Company/source snapshot and incoming profile ID/version exactly match the
  stored request. An absent profile does not bypass that binding.
- The later candidate differs, still belongs to the same company/source, and is
  still the job's selected candidate when reread after acquiring the request lock.
- Existing source checks and `native_job_link()` admit the complete persisted
  native observation. Disabled sources, changed saved profiles, mismatched
  snapshots, unsupported providers and shared ATS origins remain inadmissible.

The original request ID, creation time, profile snapshot, accumulated reasons and
maximum-score policy remain. The candidate/job replacement, audit entry and
existing queue transition commit together. The audit records prior/new candidate
and job IDs and the resulting revision. That revision advances once, so an old
manual-review submission cannot authorize a different binding. Queue failure
rolls back the replacement and audit. Repeated or concurrent intake yields one
operation.

This evidence refresh preserves the binding of authorized, queued, running,
terminal and dismissed requests. The separate source-configuration renewal path
retains its existing behavior. This refresh does not replay an operation or
revise user approval.

On PostgreSQL the fresh job read takes `FOR KEY SHARE SKIP LOCKED`, the lock
needed by the changed foreign key. A competing collector's job lock therefore
causes deferral instead of waiting while holding the request. The parked intent
and its binding remain available; the collector or a later eligible intake can
queue it. This avoids adding blocking batch/profile lock orders. Workers still
validate current profile/source/proof authority before employer egress.

## Evidence and acceptance boundary

Synthetic regressions cover real source collection through durable requests,
high-priority outbox admission, stale review rejection, Store reopen, replay,
invalid evidence, profile fences, dismissal and preserved active/terminal work.
A controlled boundary interleaving tests selected-candidate drift. Disposable
PostgreSQL tests separately exercise reopen, transaction rollback and two actual
concurrent intake transactions producing one operation and one refresh audit.

Admission performs no employer request and grants no verified domain. The queued
native link remains `ATS_JOB_LINK_CANDIDATE`; existing bounded employer-page
reads and reciprocal corroboration are still required before domain authority.

MJ-REQ-264 remains PARTIAL. This repairs later usable evidence for the same parked
profile/source binding. General origin discovery, other native providers and
resnapshotting across different profile revisions remain outside this slice.
No real employer submission, paid provider call, migration or credential change
is introduced.
