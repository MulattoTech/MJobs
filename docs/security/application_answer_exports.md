# Private application-answer exports

MJ-REQ-171 covers contact information, resume content, employment history,
application answers and session data. This change protects one file-storage
boundary within that requirement; it does not establish whole-product PII
acceptance.

## New exports and interrupted writes

Reviewed answer exports use an exclusively created, unpredictable temporary file
in the destination directory. On POSIX filesystems, its mode is owner read/write
(0600) from creation, even when the process umask permits group or other access.
The exporter flushes and fsyncs the content before atomically publishing it.
Publication retains the temporary file's permissions. New application, answer
and version directories request owner-only access (0700); existing directories
are not recursively changed.

A process death during writing can leave a private temporary file. It cannot
publish partial content or commit a document record. A subsequent export can
proceed independently without deleting that orphan or unrelated files. Directory
creation failures use the same fixed storage error. If a failed write also cannot
remove its temporary file, the private orphan is retained and cleanup does not
mask the original classification.

## Recovery of a completed but uncommitted file

Recovery opens the content-addressed path without following a final symlink
where supported. It accepts only a regular file with one hard link and exactly
the expected bytes and checksum. Reads are limited to the expected size plus
one byte; special files are rejected before reading.

On POSIX, the exporter then restricts that exact file descriptor to 0600 before
recording the document. A permission failure returns the fixed
`ANSWER_EXPORT_STORAGE_UNAVAILABLE` classification and leaves the database
uncommitted. A mismatched file is preserved without changing its bytes or mode.
A hard-linked file is rejected to avoid changing an unrelated file's permissions.
Existing path validation continues to reject symlink and Windows junction aliases.

## Scope and limits

This is forward protection plus validation of a file encountered during a
specific interrupted-export recovery. It does not scan or rewrite historical
exports, backups, unknown directories or unrelated data. Operators should retain
the document volume's existing access controls. Windows uses its configured
directory ACLs; POSIX mode checks are not proof of Windows ACL isolation.

Profile-scoped downloads, immutable hashes, approval checks, current avoidance
policy, no-store responses and disabled external submissions retain their
existing contracts. The exported answers are still user-reviewed text; this
change neither generates answers nor submits an application.

## Regression coverage

`tests/integration/test_answer_export_privacy.py` exercises permissive umask,
a real child-process exit during a flushed write, matching-file recovery,
permission denial, conflicting bytes, hard links, failed flush cleanup and
profile-scoped download after reopening the database. POSIX-only tests are
explicitly skipped on native Windows and must run on Linux before acceptance.

Adjacent answer-export, path-security, story-citation and durable-review suites
cover existing behavior. Exact source revisions, executed commands, failures,
skips and protected checks belong in the sprint evidence and PR receipt.
