# Browser session directory admission (MJ-REQ-171)

Persistent browser profiles contain session data. New `browser-profile` roots
and selected named child directories are created explicitly with mode 0700.
This avoids the intermediate-directory default left by `mkdir(parents=True)`
when a named profile is selected.

On POSIX, both existing session-bearing levels must be real directories with no
group/other permission bits. The check runs during initialization and read-only
lookup, including manual authentication. Symlinks and junctions remain refused.
Existing profiles are never chmodded, deleted, copied, or changed to another
selection. A permissive profile is refused with `BROWSER_PROFILE_UNSAFE`;
manual-auth consumers retain their fixed unavailable/stale outcomes and do not
launch a browser. Filesystem failures are also reduced to that fixed error
without returning a private path.

Application/output ancestors retain their existing modes. The configured
selection stays pinned, the backup exclusion root remains `browser-profile`,
and read-only lookup of an absent profile creates no state. Private legacy
profiles and recovered selections remain supported.

## Rollout and recovery

This is an intentional fail-closed change for existing permissive POSIX browser
profiles. Before rollout, the release owner should assess the modes of the
affected MJobs profile directories without reading cookie content. Sessions
with group/other access require an explicitly reviewed owner-side remediation
before reuse. Do not silently repair or discard an existing session to make a
test or release pass. There is no database migration or bulk permission rewrite.

A protected revert restores the previous admission behavior; it also restores
the previous privacy gap. The patch itself never rewrites stored session data.

## Verification and limits

The synthetic privacy regressions cover default/named creation under an open
umask, legacy and pinned refusal without mutation, directory type checks,
read-only behavior, storage-error sanitization, and replacement after creation.
Manual-auth entrypoint regressions assert that a permissive session cannot
launch a browser. Existing configuration, deadline, backup/recovery and
path-containment fixtures remain part of the focused regression suite.

POSIX mode checks do not certify Windows ACL isolation, extended ACLs, directory
ownership, a hostile process replacing ancestors, or permission changes after
admission. Child cookie files are protected by the enclosing private session
directory; this is not a recursive file/ACL audit. Full MJ-REQ-171 remains
PARTIAL, and merge, deployment and whole-requirement acceptance need separate
evidence.
