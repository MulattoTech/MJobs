# MJobs Mission Control — one-click web launcher and release tracker

## Start the app

Double-click **MJobs Mission Control** on the Windows desktop. The shortcut invokes the checked-in `Launch-MJobs-MissionControl.vbs` (hidden; no console flashes) and `Start-MJobs-MissionControl.ps1`, which starts a loopback-only FastAPI dashboard at **http://127.0.0.1:18726/** if it is not already running, then opens that page in the default browser. Running again opens the existing instance. The bootstrap and server logs stay under the local common Git directory `.git/mjobs-coordination/`; they are not committed.

To start manually:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs-MissionControl.ps1
```

**No CLI sessions are started merely by opening the dashboard.** The user must click **Launch CLI** on each card.

## Three supported launch slots

| Web slot | Installed runner | Isolated checkout | Scope |
|---|---|---|---|
| Codex | `codex exec` | `C:\Tools\Dev\MJobs-Codex-App` | review-first application automation |
| Antigravity | `agy --print` | `C:\Tools\Dev\MJobs-Antigravity-Matching` | evidence-backed AI matching |
| Third CLI | `claude --print` | `C:\Tools\Dev\MJobs-Claude-Delivery` | independent local delivery/test acceptance |

Profiles and models are **both selected from dropdowns**; the user never needs to type a CLI model ID or assemble flags. Each CLI has a **Refresh models** action and independent **Reasoning / effort** selector constrained to its real model metadata. Codex obtains visible IDs and supported efforts from its installed `codex debug models` catalog; Antigravity obtains model IDs/titles from its installed `agy models` command; Claude uses the model aliases documented by its installed CLI. When Antigravity embeds effort in the selected model ID, the selector locks that effort and omits a redundant CLI effort flag. On provider discovery failure, the last known catalog is labeled STALE; if no catalog exists, launch is disabled. A listed model is not a guarantee of plan entitlement.

**Agent / session name** has functional presets and a **Custom name…** option. These are human-readable session labels included in the agent instructions—not unverified CLI-native `--agent` identifiers that could prevent launch. Each card still offers **Read prompt**, **Launch CLI**, **Stop**, optional extra instructions and opt-in auto-continue (off by default). A manual model refresh is authenticated with the local Mission Control token and never starts an AI session.

The legacy Ollama/OpenCode lane remains visible in **Advanced agent telemetry** only. When its executables are absent, the UI reports it as unavailable and never reinstalls them.

## Clean-main independence and self-reconciliation

**The launcher does not require a clean, current, or synchronized `main`.** It never resets, cleans, stashes, commits, rebases, merges, or force-pushes the primary checkout. At each explicit launch it:

1. Tries a bounded fetch of remote `main`; if offline, chooses the cached `origin/main`, falling back to locally committed main and warns.
2. Reuses the lane's designated worktree if the repository identity and branch match. Existing dirty/ignored data is preserved. A foreign directory or unexpected branch blocks that lane.
3. Creates a **missing** worktree from the selected base without switching primary `main`. A clean branch that is only behind may be fast-forwarded. Dirty or diverged branches are left intact, with the warning shown to the user.
4. Records the chosen model, agent, profile and prompt in **local** common Git dashboard settings; starts the exact installed CLI with bounded local worktree context.
5. Instructs the launched model to check real GitHub issues/claims, take a unique path-scoped claim, and wait for the sole integrator to update accepted requirements and merge to main. No hidden automatic integration.

A launched CLI is **not** a license to override another writer's claim. Claim collisions block that work slice. The new Claude lane has its own claim namespace. Antigravity and Claude can now use unique child/session claim IDs without overwriting pre-existing legacy claims.

## Current release acceptance

The Release Acceptance panel uses `tools.verification.progress_dashboard.snapshot()`, **the same canonical** `planning/CAPABILITY_MATRIX.json` and `planning/REFERENCE_GAP_MATRIX.json` that drive the portable ChatGPT skill. It displays only whole requirements with fully accepted evidence, side-by-side counts of PARTIAL and NOT_RUN, acceptance by area and priority, latest matrix timestamps, current milestone and separate release-readiness state.

The browser refreshes its read-only status approximately every 11 seconds while visible. The remote `main` SHA is checked on a short cache to avoid excessive network requests. `checkpoint_current` distinguishes a rendered GitHub/Markdown/SVG checkpoint from live local matrix changes. **The page never edits matrices, fabricates successful tests, or automatically publishes to GitHub.** An integration owner must refresh/publish accepted checkpoints according to `docs/PROGRESS_TRACKING.md`.

## Safety and operational notes

The service binds only to 127.0.0.1 and checks its own health identity before opening a port. Mutation endpoints require a per-process token and same-origin requests. Process runner names are allowlisted and launch profile files are fixed repository-relative entries, not arbitrary executable paths. If a CLI is already active in a launch slot, Start refuses to take it over; use Stop and launch again after verifying the prior work is settled.

CLI processes may consume account/API quotas when manually launched. Their availability can differ from a listed model name. Preserve logs and actual validation; hosted CI/reviews and external real job submission remain OFF.

Run verification:

```powershell
python -m unittest tests.unit.test_mission_control tests.unit.test_agent_dashboard_api tests.unit.test_agent_dashboard tests.unit.test_lane_coordination -v
python -m tools.verification.progress_dashboard --check
python -m tools.coordination.lane_coordination validate
```


## Exact pre-launch prompt review (October 10 reliability fix)

**Read prompt opens a dedicated, scrollable review window.** It displays the
complete effective prompt as assembled by the same supervisor logic that will
execute the CLI; it includes Mission Control's safety/ownership instructions,
the selected versioned profile, and any additional user instructions. The
model, reasoning effort, agent/session label, auto-continue policy, CLI
arguments (with the prompt itself redacted), and intended worktree are
available before launch.

The original control displayed only the profile file in a collapsed inline
section. This was not a reliable reflection of actual CLI execution.

The new preview uses POST /api/launcher/{lane}/preview with the same local
same-origin token required by Launch CLI. The endpoint is read-only: it does
not start an AI, invoke a model, create a worktree, change CLI settings, or
acquire an integration lock. It validates the selected CLI-model/effort pair
and refuses stale model catalogues until manually refreshed.

**Launch CLI remains locked until the exact current preview is approved.**
Click Read prompt, inspect the entire prompt, and click Approve reviewed
prompt. Any change in profile, model, reasoning effort, agent name, extra
instructions, or auto-continue clears approval immediately, regenerates the
effective preview, and requires reapproval.

The inline Selected prompt contents display and the large review window
refresh together. Both opening and closing the large viewer and expanding or
collapsing the inline disclosure refresh the view. In-flight response
versioning prevents an older request from replacing the latest selection.

For safety, the server binds an SHA-256 preview fingerprint to the complete
prompt, selected options, intended worktree and command arguments. The
actual launch reassembles these data and rejects missing/stale/mismatched
approvals **before creating or modifying any claimed worktree**. The
supervisor also compares the final prompt bytes again immediately before
starting the subprocess. No profile editing or race condition may silently
substitute a different prompt.

**Preview does not guarantee model entitlement or successful external
authentication.** The local installed CLI may still reject its own model
selection or encounter a provider quota at execution. Normal MJobs Git
claim exclusivity and external-submission-off policies continue to apply.

Verification without launching any real CLI:

    .\.venv\Scripts\python.exe -m unittest tests.unit.test_mission_control -v
    node --check tools\agent_dashboard\static\mission.js

A full browser scenario additionally tests model/profile/effort switching,
editable instructions, closing/reopening the prompt, approval invalidation
and absence of launch network requests. Never click Launch CLI during a
smoke test.


## Safe rollout while main's integration lock is held

If the root Codex integration lock is active, do not copy these files into the
live MJobs main checkout. Publish the verified branch and install a separate
versioned code checkout instead:

    git clone --single-branch --branch fix/mission-control-prompt-preview-20261010 https://github.com/MulattoTech/MJobs.git C:\Tools\Dev\MJobs-MissionControl-Launch

The entrypoint in that checkout uses the *actual* MJobs repository for data,
issue claims, prompts, worktrees, and model catalogs without changing main's
tracked files. Once the prior Mission Control dashboard is confirmed to have
no supervised CLIs running, stop only its own server process, then start:

    C:\Tools\Dev\MJobs-MissionControl-Launch\Start-MJobs-PreviewSafe.ps1 -RepoRoot C:\Tools\Dev\MJobs

To create or update the desktop shortcut with a backup of the old shortcut:

    C:\Tools\Dev\MJobs-MissionControl-Launch\Install-MJobs-PreviewSafeShortcut.ps1 -RepoRoot C:\Tools\Dev\MJobs

The launcher requires health API v3 with the exact-prompt and mandatory
approval capabilities; it refuses to reuse an old or unrelated server and
**never kills agents or forces a port takeover**. The shortcut installer
backs up the old shortcut under LOCALAPPDATA/MJobs/backups. Both scripts
are versioned, support other Windows checkouts, and do not start coding agents.

After a permitted integration checkpoint, the same code can be included in
main by an authorized integrator and the standalone code checkout retired
only after inspecting the active dashboard/runner ownership. Do not replace
the primary checkout while it is dirty or locked.


## Live development history timeline (October 10)

The current Mission Control includes a **Development history** panel immediately
after Release acceptance. It restores and expands the earlier read-only
monitor's Git commit timeline into the same application as the reviewed
three-CLI launch studio.

### What appears

- Newest 100 local commits are scanned; the default view displays 50.
  Choose 25, 50, or 100 and filter by work category.
- **Core features** mean multi-component product implementations based on
  committed application/backend/service/storage paths and amount of code
  changed. **Product features** are narrower implementations; **bug fixes**
  and **other product changes** have separate categories.
- **Tests**, **documentation**, **bookkeeping** (planning/evidence/checkpoints),
  and **engineering operations** are separately colored.
- Each entry shows actual commit SHA, subject, commit time, a likely affected
  subsystem derived from file paths, files changed, additions/deletions,
  optional issue references and an expandable exact list of affected files
  and the recorded Git author.
- The top summary shows implementation-vs-bookkeeping proportions and a
  compact colored distribution bar. It also shows the latest recorded
  Codex root model turn when local rollout metadata is available.

Git commit is NOT equivalent to model turn. Codex session turns and Git
commits do not encode a reliable author-to-commit mapping. The panel reports
Codex root turn activity separately and never claims that one named model
authored a particular commit merely because its timestamps overlap. An
arbitrary feat label is not enough to establish a major, fully accepted
product feature. For authoritative acceptance use the independent Release
acceptance matrix above the history timeline.

### Frequent, inexpensive refresh

History automatically polls every five seconds while the dashboard tab
is visible, with an immediate check when it becomes visible again. The
backend uses short, read-only, no-optional-locks local Git queries with
a five-second TTL. Model-session metadata refreshes on a separate 25-second
cache. No AI tokens, Git commits, background model tasks, GitHub Actions,
or new tracker bookkeeping are generated by this dashboard polling.

The independent GET /api/history?limit=50 endpoint accepts a read-only
commit-depth limit of 1-100. An optional refresh=true forces an immediate
re-read. The API never invokes a model, creates worktrees, takes claims, or
updates progress files. It reads the actual main checkout specified as
the launcher RepoRoot, not the separate Mission Control code checkout.
The history feed preserves expanded file details and scroll position between
refreshes when the visible records have not changed. A fresh HEAD triggers a
small new local commits notification.

The fast history feed is distinct from the slower release matrix and model
catalog refresh, avoiding gratuitous CLI discovery or inference work.

### Portable installation and verification

The source is versioned in GitHub alongside
tools/agent_dashboard/work_history.py,
tools/agent_dashboard/static/history.js, its CSS/HTML, and
tests/unit/test_work_history.py. The current safety-checking launcher
requires Mission Control API v4 with history and mandatory prompt
approval. If an older dashboard still occupies its port, the startup script
will refuse to take it over rather than silently show a stale UI.

    .\.venv\Scripts\python.exe -m unittest tests.unit.test_work_history -v
    node --check tools\agent_dashboard\static\history.js

Browser smoke testing can run against an isolated Mission Control process,
without clicking Launch CLI and without changing main.


## Visible terminals and Codex subagent staffing (October 10)

Mission Control v5 fixes the previous invisible seven-second Codex run. The
prior launcher passed a multi-line prompt through the Windows command line;
the observed Codex log contained only the initial selection line, so the
agent merely acknowledged the model. The installed Codex CLI explicitly
supports a bare codex exec - argument, reading the complete instructions
from standard input. The reviewed prompt is now carried in a short-lived,
machine-local manifest and piped verbatim via stdin to Codex.

### Launch modes

Each CLI card now has a CLI visibility selector:

- Visible Windows terminal (default): creates a real, separately visible
  Windows console with the actual Codex/Antigravity/Claude subprocess and its
  live stdout. The output is simultaneously mirrored into Mission Control's
  local logs. A normal finish remains on screen for about 25 seconds for
  inspection; then its supervisor status transitions to the real exit status.
- Headless: executes the same supervised CLI in the background. Output
  still streams into the local log without a visible window.
- View console / last output: opens a separate read-only live-tail window
  for an already-running or previously completed lane; it NEVER starts a
  second coding agent, changes its working tree, or changes its launch mode.
  Close that viewer with Ctrl+C without interrupting the agent.

The supervisor owns the one approved CLI process (and its child-process
tree), records the launcher process ID, and gracefully stops only that
supervised lane when explicitly requested. An unrelated Codex Desktop/TUI
session or external child not owned by Mission Control must not be terminated.
The visible console is output from noninteractive codex exec, not a
native interactive Codex TUI; for that, use Codex separately.

### Subagent limits — Codex lane only

Two distinct numeric options are displayed on the Codex card:

- Maximum concurrent subagents (0-50): hard native Codex configuration
  limit per launched root session, excluding that root. Passed using
  -c agents.max_concurrent_threads_per_session=N with native multi_agent
  enabled. When zero, use -c features.multi_agent=false.
- Intended active subagents (0 through maximum): the root's desired
  delegation target when safe and genuinely independent work is available.
  This is inserted into the full reviewed prompt. It is NOT a scheduler
  guarantee, forced quota, or reason to invent filler work. Zero also
  disables native multi-agent execution for the launch.

Defaults: maximum 50, intended 8. The user can choose 20/50 or solo 0/0
before reviewing/approving. These are session-local overrides and do
NOT change the user's global ~/.codex/config.toml, other sessions, or
machine-level resource limits. No account-limit bypass or unattended
background auto-continue is implied; the separate explicit auto-continue
option remains off by default. Children follow configured default child
models and required path/issue ownership, not necessarily the root model.

### Prompt/review guarantees

The full prompt review includes selected model, effort, agent/session name,
visibility mode, native maximum and intended target. The CLI preview shows
the exact argv (with an approved prompt via stdin placeholder in place of
private text). Every change to these settings invalidates approval. The
backend compares the full preview fingerprint again before launch, and the
supervisor checks the full prompt SHA before spawning the CLI. A malformed
target or an intended count above maximum is rejected before creating any
worktree.

The manifest containing private prompt text is unversioned, created inside
Git-common local coordination, and removed as soon as the wrapper reads it.
It is never committed, synced to GitHub, or exposed on a command line.
Existing Git claim / lock and disabled external job submission rules remain
unchanged.

### Verification

Run local, no-provider tests:

    .\.venv\Scripts\python.exe -m unittest tests.unit.test_mission_visible_terminal -v
    .\.venv\Scripts\python.exe -m unittest tests.unit.test_mission_control -v
    node --check tools\agent_dashboard\static\mission.js

The synthetic end-to-end stdin test sends a deliberately complex multi-line
prompt to a local stub instead of any paid model, verifies the exact bytes,
and confirms the prompt manifest is removed. The unit tests verify the real
CREATE_NEW_CONSOLE flag and checked capacity flags without launching a
coding model. Actual Codex tool-use results and subagent occupancy depend
on the installed CLI, its account capacity, and the active issue backlog.
