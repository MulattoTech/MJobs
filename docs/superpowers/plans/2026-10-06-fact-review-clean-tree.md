# Fact Review Clean Worktree Evidence Guard

## Objective

Prevent the local fact-review browser verifier from returning PASS with a Git HEAD SHA that does not include the tested source changes.

## Acceptance behavior

- After resolving HEAD, query tracked and untracked worktree state with a shell-free, five-second-bounded Git call.
- A nonempty porcelain status returns no source commit and the fixed `GIT_WORKTREE_DIRTY` category; filenames and raw Git output are never written to evidence.
- Git status timeout, launch failure and nonzero exit have stable safe categories, and the verifier writes FAIL evidence.
- A clean checkout still records the full exact HEAD SHA and can pass the browser oracle.

## Implementation and validation

1. Add a failing oracle where HEAD resolves but Git reports changed tracked/untracked files.
2. Add the bounded status call and fail-closed evidence behavior.
3. Test dirty, clean, timeout, launch and command-failure cases; run the actual four-case browser verifier only from a clean integrated checkout.
4. Run repository validators and the full local suite; record all failures/skips and exact hashes. Keep issue #5 OPEN/PARTIAL.

## Boundaries

No files or paths from `git status` are included in receipts. Hosted verification and review remain NOT_RUN by policy.
