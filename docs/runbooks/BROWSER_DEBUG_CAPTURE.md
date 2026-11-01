# Optional browser failure diagnostics

Set `MJOBS_BROWSER_DEBUG_CAPTURE=1` in the supported private runtime environment
to enable failure diagnostics. Other values leave capture disabled. The flag
applies to mock application automation, controlled provider dry runs and manual
authentication. The isolated manual-auth host receives only a validated boolean
through its existing bounded private input channel.

On a browser exception, the owner attempts capture before closing its context.
A successful dry run or normal user closure of the sign-in window produces no
failure package. Diagnostic errors never replace the original browser failure,
enable retries or authorize a submission.

Each application's `debug-captures` directory contains randomly identified
attempt directories. A completed package records a fixed phase name, sanitized
current URL, sanitized DOM topology, bounded console/page/network error
fingerprints and a redacted screenshot when available. Treat these local
diagnostics as private operational data and review them before sharing.

## Privacy and retention

URL credentials, path values, query strings and fragments are discarded. DOM
snapshots retain only bounded topology and fixed tag/role names; they omit text,
control values, arbitrary attributes, subframes and shadow roots. Error messages
are replaced by keyed fingerprints and bounded lengths. Console listeners retain
only error-level events. Screenshot masking follows the shared capture policy,
including credentials and populated controls.

The shared application capture root retains at most 20 managed packages for up
to 30 days, pruning on capture. The DOM file is capped at 8 KiB, 160 visited
nodes and depth 8; the screenshot is capped at 5 MiB and the event list at 50.
Symlink or invalid attempt destinations are rejected, and incomplete packages
are removed when the capture budget or caller cancellation interrupts work.

## Operational limits

DOM and screenshot work share a maximum two-second capture budget, shortened
by the caller's remaining deadline. Native DOM protocol work has a 300 ms
sub-budget and bounded cancellation. An unavailable or unsupported transport is
recorded as omitted rather than falling back to executing page JavaScript.

These are diagnostic bounds, not full acceptance of MJ-REQ-004. Raw CDP replies
are decoded before retained DOM limits apply, local filesystem operations remain
synchronous with checkpoints, and the browser owner remains responsible for
context cleanup. Controlled fixture coverage does not establish live employer
adapter support or authenticated production access.
