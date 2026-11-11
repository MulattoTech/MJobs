# Fleet controller moved

Controller source and tests are maintained in `MulattoTech/Fleet-Control-Hub`.
Do not recreate a second controller here. This application is a project managed by that hub.

The migration preserves existing state directories, port numbers, quota guards, and project scope.
See https://github.com/MulattoTech/Fleet-Control-Hub/tree/main/packages for source.

## Manual MJobs startup after synchronized work

The native fleet operator console is at `http://127.0.0.1:4320/fleet/` (direct controller fallback: `http://127.0.0.1:8787/`). The retired port 18729 is not a dependency. Opening or refreshing either console must not launch agents.

1. Update the canonical MJobs `main` worktree using a fast-forward-only pull and check that outstanding PRs, branch ownership, and preserved session mappings are reconciled. Do **not** erase the saved fleet or overwrite live runtime state during synchronization.
2. On the **Start MJobs** card, review the live allowance and the separately editable **Start / new-task reserve (%)** and **Emergency auto-stop cutoff (%)** fields. **Save limits** persists policy without starting anything.
3. A saved emergency cutoff is not automatically armed. If you want automatic session termination, open **Usage**, choose **ARM AUTO-KILL**, and explicitly confirm **ARM AUTO STOP**. The saved reserve can pause *new assignments* before the emergency cutoff is reached.
4. When the controller reports fresh usage and a ready/paused fleet, click **Start MJobs** yourself. Do not use advanced Restore to circumvent a locked or uncertain Startup owner.
5. Verify the active mission/dispatch state after clicking. Pause or emergency-stop remains an explicit operator control.

The Hub source pin is maintained in [`source.json`](source.json). CI must check out and test that **exact** signed Hub revision; it must not silently use a separate hard-coded commit. The live controller has stronger local safeguards than some historic staged source files. Until [Fleet-Control-Hub issue #17](https://github.com/MulattoTech/Fleet-Control-Hub/issues/17) is reconciled, **do not bulk-install** a Hub package over the live controller; inspect each differing file with backups.
