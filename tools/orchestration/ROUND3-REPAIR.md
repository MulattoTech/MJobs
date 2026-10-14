# MJobs Round 3 launcher repair — 2026-10-14

## Confirmed failure
The local GitHub CLI reports an invalid saved login. `gh api --hostname github.com user` returned HTTP 401, while `gh project view` reported the less useful `unknown owner type`. The separate ChatGPT GitHub connection can still read MulattoTech/MJobs. No evidence established whether the CLI credential expired, was revoked, or became unavailable.

The launcher now verifies authenticated identity and Project #1 access before creating a clone or starting a model. A real authentication error is not retried or replaced by a stale queue. Only bounded transient read failures are retried.

## Resume after browser authorization
Run from C:\Tools\Dev\MJobs:

```powershell
pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -RepairAuth -Preview
```

Complete the GitHub browser/device authorization as MulattoTech. The standard GitHub CLI flow requests Project and workflow access for the repository workflow. Credentials stay with GitHub CLI; do not paste tokens into chat. Environment token overrides are detected, not silently replaced.

After the preview succeeds, choose either:

```powershell
# Native Codex TUI; exit stops this session rather than restarting it.
pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Interactive

# Automatic, bounded, fresh-context iterations with logs.
pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1
```

Both paths select only eligible P0/P1 Project issues. They do not reprioritize issues or infer acceptance from a PR merge. In-progress cards are not automatically taken over. Original completion claims are checked against the matrix on an exact published main revision.

## Other repairs
- Automatic execution uses the installed CLI's `--approve-for-me` path and normal approval review. It does not use `approval_policy="never"` or disable sandboxing. In this CLI, `--sandbox` and `--approve-for-me` conflict; they are no longer passed together.
- Explicit end-of-input prevents `codex exec` from waiting indefinitely for inherited redirected stdin.
- Directives, queue snapshots and output schema/results are inside the independent working clone under an excluded `.mjobs-run/` folder, rather than only in the original checkout's protected `.git` directory.
- A shared launcher lock also checks legacy runner locks; it does not terminate unrelated owners.
- Checkpoints are written atomically, errors remain visible, and process-local UTF-8 output is used.
- Native TUI mode is separate from JSONL mode. Configured agent limits are not evidence that those agents actually spawned.

## Verification
- PowerShell regression tests: **21 passed, 0 failed**. Coverage includes authentication, permissions, GraphQL errors, bounded read retries, no mutation retries, strict priority filtering, incomplete/duplicate queues, redaction, Unicode output, timeout behavior, and serialization.
- Complete launcher control-flow harness: **8 passed, 0 failed**. It used real local Git repositories but stubbed GitHub and Codex. It tested preview, preparation, automatic dispatch, native interactive routing, status, scope filtering and preflight failure. It made zero real GitHub or model calls.
- Real Codex local Git smoke: **PASS**. With standard automatic approval review, Codex created a branch, staged and committed a synthetic sentinel, and read the committed file. The independent parent verifier confirmed branch `smoke/approval-check`, sentinel `R3-SMOKE-PASS`, and commit `7fa7211b46e323f31d3b9191984380d8d162bdb2`. This was a local fixture, not a product requirement or live GitHub publication test.
- Live GitHub-backed campaign remains **NOT RUN / AUTHORIZATION REQUIRED** until the CLI browser sign-in completes. No P0/P1 feature completion is claimed by this launcher repair.

## Local evidence and preservation
Original launcher backup: `C:\Tools\Dev\MJobs\.git\mjobs-launcher-repairs\20261014T180724Z\Start-MJobs-P0P1-Round3.ps1`.
Live smoke evidence: `C:\Tools\Dev\MJobs-Launcher-Smoke-20261014-131458\verification.json`.
The prior blocked worktree, its pending merge and four closeout documents were not edited. No VS Code restart, host reboot, global Git trust exception, ACL repair or authentication-token extraction was performed. Only a hung process tree created by the smoke test was terminated.

## Repeat offline checks
```powershell
pwsh -NoProfile -File .\tools\orchestration\Test-MJobsRound3.ps1
python .\tools\orchestration\Test-Round3-Flow.py
```
The live smoke script is optional and uses one actual Codex task. Do not run it as part of routine offline regression checks.
