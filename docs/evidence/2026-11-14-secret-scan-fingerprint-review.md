# Historical secret-scan fingerprint disposition (2026-11-14)

Scope: precisely seven `generic-api-key` findings from the Git histories of PR #1028 and PR #1030. These are generated evidence identifiers, not API credentials. The repository's normal Gitleaks scanner and all other findings remain enabled; the companion `.gitleaksignore` entries pin each **commit, path, rule and line**.

## Independently checked items

- `6fda41901137f91961837908a1a64526ee0cc075:docs/evidence/2026-11-14-answer-resume-currentness-8966724d.json:18`: value resolves via `git cat-file -t <value>` to a Git **blob**.
- Same commit and path, line 35: a 64-character `baseline_input_sha256` value for `tests/integration/test_application_answer_resume_api.py`, recorded as a SHA-256 of the dirty pre-implementation test input; independent review reconstructed the 19,447-byte baseline. The evidence explicitly marks this input as uncommitted, and no credential-like context is present.
- `00688bcf09cfb4aefb6ab55ae5df9f25ff5a68a4:docs/evidence/2026-11-14-browser-profile-metadata-ef18.json`: values on lines 134, 135, 137, 140 and 141 each resolve to Git **blob** objects under `git cat-file -t`.

To independently reproduce the Git-object checks, extract the exact value at each stated line from `git show COMMIT:PATH` and call `git cat-file -t VALUE`. The reviewed values are 40-character Git object identifiers; the remaining 64-character value is an explicitly labeled test-input SHA-256 in the evidence JSON. The checked commit/path/line triples are immutable.

## Scope and controls

Only these seven full fingerprints were added. No `--all` option, detect rule, CI workflow, broad allowlist, path pattern, or severity threshold was changed. This review does **not** certify the remainder of the repository or suppress an unknown finding. CI must still run the full history scan after merge.

This remediation is distinct from production Fleet proxy repair and acceptance evidence. It changes neither `planning/CAPABILITY_MATRIX.json` nor `planning/REQUIREMENT_INDEX.json` and makes no new acceptance claims.
