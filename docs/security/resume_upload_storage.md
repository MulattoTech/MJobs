# Uploaded-original private storage admission (MJ-REQ-171)

Uploaded resumes and other original documents contain sensitive employment data.
On POSIX systems, the upload service now refuses an encountered uploads
directory or original whose mode grants any permission to group or other users.
The check applies to the `uploads` child, not to every ancestor: a document
volume mounted at a 0755 parent remains compatible with a private 0700 child.

## Admission and compatibility

- Newly created upload directories request 0700. Fresh temporary originals start
  private and new published originals retain 0600.
- Existing directories and originals are never silently chmodded. A permissive
  original or uploads child receives a fixed `UploadIntegrityError`; current
  upload/download API routes return 409 without exposing a filesystem path.
- An original must be a regular file. POSIX privacy admission checks
  `stat.S_IMODE(st_mode) & 0o077`, so private 0400 originals remain readable.
- Private multiple-link originals remain supported. Fresh publication uses a
  hard link and temporarily has two links before staging cleanup.
- Repeat uploads and reads use the same descriptor-based reader. It rejects
  invalid recorded sizes before opening an original, admits only 1 byte..10 MiB,
  reads at most the expected size plus one byte, and checks exact size/hash.
- POSIX opens use a checked uploads-directory descriptor, a relative final open,
  no-follow flags where supported, and nonblocking open before regular-file
  validation. Mode/type/size checks inspect the opened descriptor before reads.
  Descriptor modes and sizes are checked again before returning bytes.
- Original format authority, filename/media type, profile scope, immutable
  content hashes, series/revision idempotence and mixed-extension concurrency
  remain unchanged.
- Filesystem failures use fixed messages. If cleanup also fails after an
  integrity refusal, the initial refusal remains authoritative and any orphaned
  staging file stays private. A cleanup-only failure after metadata commit can
  return an error with an already committed private original; idempotent retry
  remains the recovery path.

Windows retains descriptor/type/size/hash checks and normal upload behavior.
POSIX mode assertions do not establish Windows ACL isolation.

## Focused verification

The exact initial regression source is retained at
`docs/evidence/2026-11-03-webpro-642e4b-resume-upload-privacy/baseline-test-source.py`.
On protected source 63cb1042, four synthetic SQLite/API cases failed as expected:
directory-only 0755 and file-only 0644 admission, independently through reopened
download and repeat upload. Bytes, modes, rows/revisions and facts were
unchanged before each failed refusal assertion. Raw inputs, command and console
are retained in the owned verification output; the published baseline receipt
records their hashes.

Run the focused fixtures in a POSIX runtime to verify the permission boundary:

```text
python -m unittest -v tests.integration.test_resume_upload_privacy tests.integration.test_resume_upload
```

The fixtures also cover private 0400 files, fresh restrictive modes under a
permissive umask, private two-link originals, fixed storage errors, cleanup
failure, final symlink substitution and FIFO refusal. Native Windows positive
controls are separate; POSIX skips are not permission acceptance.

## Limits

MJ-REQ-171 remains PARTIAL. This is encountered-upload admission, not bulk
historical-file remediation, a filesystem monitor, ACL analysis, or a guarantee
against a hostile actor changing ancestors or permissions after admission.
No existing applicant files, deployment volumes, historical backups, contact
records, sessions or production database are scanned or rewritten. Full PII
acceptance, exact-source execution, protected integration and deployment
readback remain separate evidence.
