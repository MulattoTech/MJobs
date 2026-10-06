# Live VS Code Feedback Development

## Start with the Development & Feedback page

Double-click **Start-Feedback.cmd**, or run the VS Code task
**MJobs: Development & Feedback**. The launcher opens
http://127.0.0.1:18702 and reuses the matching local server when already running.
It needs Python, but does not need Docker, the MJobs API, or a frontend build.

Choose any of the 20 tasks on the page. Each has its own questions; additional
details are optional. Drafts save as you type, and **Saved work** restores them.
Closing the browser or restarting the server does not discard saved drafts.
The status distinguishes browser-only recovery from a confirmed computer save.
Only one editing tab per browser workspace is allowed, preventing competing
tabs from overwriting browser recovery data. Separate browsers receive a
conflict warning and can preserve both versions as separate drafts.

Click **Ready for Codex** when the form is complete. This creates the local
`feedback.md`, `session.json`, and `events.jsonl` files and updates `LATEST.txt`.
Tell Codex **process my latest feedback**; no template editing or prompt copying
is required. The page saves the request; it does not start an agent, execute
commands, publish to GitHub, or grant automatic approval. Editing a ready report
creates a new draft and preserves the previous report in history.

Saved reports without a separate recorder session explicitly have
`capture_status: NOT_RUN` and zero recorded browser events. Existing recorded
sessions are suggested in applicable evidence fields; they are referenced, not
copied or overwritten. Use the optional recording workflow below when click or
error diagnostics would help.

All reports, browser recovery data, and the SQLite draft database are private
local data. The disk files live under ignored `outputs/live-feedback/`; the
launcher supports `-OutputRoot` for an explicit alternative. Keep the same
output directory and browser origin when relaunching. Browser-only recovery
depends on retaining that browser's site storage. The MJobs PostgreSQL/document
backup does not currently include this separate feedback directory.

If the page closes, double-click **Start-Feedback.cmd** again. If the computer
save fails, keep the page open and use **Save now** after recovery. Port conflicts
with another workspace are reported rather than silently connecting to it.

## Guided product review

When the goal is to evaluate MJobs as a useful product rather than troubleshoot one known defect, use **MJobs: Guided Product Review - Personal** or **MJobs: Guided Product Review - Fixture** in VS Code. The launcher starts the selected local runtime plus this feedback page and records an ignored outputs/live-feedback/ACTIVE_REVIEW.json session marker.

Follow [GUIDED_PRODUCT_REVIEW.md](GUIDED_PRODUCT_REVIEW.md) with Luna using prompts/VS_CODE_GUIDED_REVIEW.md. Luna should facilitate only: it must not edit code or create issues while you are still evaluating workflows. Create one **Review a core workflow** Ready report per distinct finding.

After the walkthrough, use a fresh turn with prompts/FEEDBACK_TO_GITHUB_ISSUES.md to create/update sanitized, dependency-aware GitHub issues. Fix them only in a later implementation turn using prompts/FIX_FEEDBACK_ISSUES.md.

## Optional browser diagnostic recording

This workflow is for iterative, user-driven MJobs development: run the real local app, exercise a workflow in a headed browser, record safe diagnostics, write what felt wrong or missing, then let Codex in VS Code turn that evidence into a test and patch.

## What the recorder captures

By default the feedback browser records only:
- local navigation paths with query strings/fragments removed;
- clicked element metadata such as tag, id, role, name, aria-label, and bounded button/link text;
- form-control interaction metadata **without values**;
- local failed requests and HTTP error status/path;
- warning/error message fingerprints instead of message text;
- session timestamps and final route.

Typed input values, passwords, tokens, resume text, and form answers are not recorded by the interaction recorder. The output directory is already ignored by Git.

## First-time setup

Use the normal MJobs workspace, normally `C:\Tools\Dev\MJobs`.

Ensure:
1. Docker Desktop is running Linux containers.
2. The repository virtual environment exists at `.venv`.
3. Python dependencies from `requirements.lock` are installed.
4. Playwright Chromium is installed.

In VS Code use **Terminal → Run Task → MJobs: Install Playwright Chromium** if needed.

## Normal PERSONAL feedback cycle

1. Open the MJobs repository folder in VS Code.
2. Run **Terminal → Run Task → MJobs: Live Feedback - Personal**.
3. The task starts PERSONAL mode on `http://127.0.0.1:18701`; real external submission remains OFF.
4. A headed Chromium window opens.
5. Perform the workflow you want to test naturally.
6. When finished, return to the VS Code task terminal and press **ENTER**.
7. The recorder writes a session under `outputs/live-feedback/<timestamp>/`.
8. Open the Development & Feedback page, choose the appropriate task, and describe what you tried, expected, observed, and want changed. Select the recorded session in an applicable evidence field, then choose **Ready for Codex**.
9. In Codex for VS Code say: **Process my latest feedback.** The page has prepared the local files. `prompts/VS_CODE_LIVE_FEEDBACK.md` remains the repository processing contract.
10. Codex should reproduce the observation with a regression test when practical, implement the smallest coherent change, validate it, and leave an exact retest instruction.
11. Run the feedback task again and repeat.

Use **MJobs: Live Feedback - Fixture** when a fictional application/submission path must be tested safely.

## Session files

A normal session contains:
- `events.jsonl` — crash-tolerant event stream;
- `session.json` — final structured summary;
- `feedback.md` — your explicit feedback;
- `outputs/live-feedback/LATEST.txt` — pointer to the newest session.

Run **MJobs: Summarize Latest Feedback** for a compact terminal summary.

## Optional visual diagnostics

The default VS Code task does **not** capture screenshots or Playwright traces because those may contain personal data.

For an explicit visual debugging session, run manually:

```powershell
.\.venv\Scripts\python.exe -m cli.live_feedback run --base-url http://127.0.0.1:18701 --visuals
```

This adds a final screenshot and Playwright trace. Treat them as private local artifacts.

Likewise, `--diagnostic-text` stores browser console/page-error text instead of hashes. Only enable it when needed because application-generated diagnostic strings may contain private context.

## What Codex can and cannot infer

The event stream shows what controls/routes were exercised and where technical failures occurred. It does not reliably know *why* you clicked something or what you intended. Your `feedback.md` is the authoritative product observation; diagnostics support it rather than replacing it.

## Development discipline

A feedback iteration should normally follow:

OBSERVE → FEEDBACK → REPRODUCE → TEST RED → PATCH → TARGETED TEST → AFFECTED INTEGRATION/E2E → EVIDENCE → RETEST

For purely visual/interaction issues where an automated RED test is not practical, Codex should write a deterministic acceptance check or browser E2E assertion instead of pretending a unit test exists.

Do not use this workflow to weaken MJobs safety gates. PERSONAL mode keeps fictional submission actions unavailable and real external auto-submit remains disabled unless separately and explicitly enabled under normal product policy.

## Troubleshooting

If Chromium is missing, run the install task. If MJobs is already running, the startup task should reuse the existing Compose project where safe. If the browser task exits before interaction, run it from a normal interactive VS Code terminal rather than a non-interactive shell. If a diagnostic session contains private visual material, delete that ignored session directory after the bug is resolved.
