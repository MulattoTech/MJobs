# MJobs complete recorded delivery history

Mission Control's **Complete recorded delivery history** panel is a read-only,
source-attributed activity record. It complements rather than replaces the
release-acceptance dashboard and the MJobs Project #1 P0/P1 delivery queue.

## Sources and semantics

| Source | What is recorded | What it does **not** prove |
| --- | --- | --- |
| Git commit history | Unique commit SHA, recorded author/time/message, local or archived-clone scope, cached-main reachability | Deployment, GitHub publication, AI authorship, feature acceptance |
| GitHub PRs | Creation, merge, and closure events for accessible repository PRs | Original requirement PASS, exact work start, full verification |
| GitHub issues | Creation and closure events; PR entries are excluded from issue history | Product completion just because an issue is closed |
| GitHub branches | Branch HEAD and additional commits reachable only through remote comparisons; reports incomplete comparisons | Branch creation timestamp or permanent coverage of deleted refs |
| Original and reference acceptance matrices | State transitions recorded in **first-parent published main** Git history, with exact commit SHA | Exact date development began, separately accepted child tasks |
| Historical acceptance captures | Changes between *observed* snapshot pairs, preserving the first observed timestamp and capture ID | Exact timestamp of the underlying commit, proof an issue was implemented |
| Current local workspaces | Independently retained clones plus registered worktrees, dirty state and observed file counts | Committed or published work; archived dirtiness is not treated as new work |

Original MJ-REQ and supplementary MJ-REF acceptance transitions are separately labeled.
An item transitioning to PASS appears explicitly in the activity feed. Committed
acceptance changes and local capture observations can report the same change;
those are **two evidence records**, not two completed requirements.

The existing green `+N` release cards are current-versus-historical accepted
count differences. The activity feed shows which requirement IDs changed,
from which status to which status, and which source observed the change.

## Viewing and exporting

Open `http://127.0.0.1:18729/` and locate **Complete recorded delivery
history**. Filter by type, type `MJ-REQ-277`, an issue ID, a branch name, or a
Git SHA into the search box, and use **Load older records**. The
**Requirements promoted to PASS** filter is the quickest way to locate a
historical acceptance promotion.

**Export all recorded metadata (JSON)** retrieves paginated activity, includes
the local workspace inventory and coverage/limitation notes, and aborts rather
than combining events from different snapshots if sources changed during export.

The backend provides:
`GET /api/delivery-ledger?kind=all&limit=60&offset=0&search=`
This endpoint does not start agents, mutate Git, push, change acceptance,
or fetch remote branches into the local clone.

## Limits and recovery

The feed covers discoverable/retained history, **not unknowable deleted data**.
Local Git commits are deduplicated by SHA across canonical refs and retained
standalone clones. GitHub PR/issue records are paginated. Remote branch
comparison can be unavailable or truncated; details are shown under
**Source coverage, branch heads and limitations**.

Existing recovery bundles, independent clones, and pending worktrees remain
under `C:\Tools\Dev\MJobs\.mjobs-recovery`. Do not delete those archives to
make the feed smaller. Archived dirty workspaces are shown in the workspace
inventory, but not falsely dated as new code changes.

The live P0/P1 model agent may run in an independent clone: uncommitted changes
appear under **Active Codex sprint and subagents**. Git Graph cannot display
uncommitted code as a commit. A separate local mirror ref
`mjobs/sprint-live` exposes actual sprint commits without merging or pushing
them into `main`.

## Reliable prompts after cleanup

Use the canonical MJobs checkout and **actually existing files**.
`tools/orchestration/prompt_inventory.py` validates required authority files
and the reviewed Codex prompt files (including inline file references) before
a campaign starts. Missing or stale local paths cause a clear
`MJOBS_PROMPT_PATH_MISSING` failure. Do **not** fabricate paths, recreate
obsolete sibling checkouts, or reset dirty work to make a prompt pass.

The prompt inventory and corresponding P0/P1 launcher changes ship together.
Until merged and updated in the canonical checkout, a new campaign should
continue from the already-running session rather than borrowing unreviewed
history files. P0/P1 eligibility remains subject to GitHub Project #1 and
its actual issue labels and acceptance criteria.
