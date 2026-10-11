# Install and safely update MJobs

MJobs is a local developer preview. Source installation is the supported route today; a green CI badge is not a full product/security acceptance claim.

## Windows quick start

Requirements: Git, PowerShell and Docker Desktop in Linux-container mode. Docker must be running. You do not need a host Python or Node installation just to use the Docker application.

```powershell
git clone https://github.com/MulattoTech/MJobs.git
cd MJobs
.\Start-MJobs.ps1
```

Open http://127.0.0.1:18700 for the fictional workspace. The startup command builds images, creates missing local configuration, runs migrations, and waits for readiness. Use `-NoOpen` to avoid opening a browser. Use `-NoBuild` only when the current source has already been built.

## Personal workspace

```powershell
.\Start-MJobs.ps1 -Personal
```

Open http://127.0.0.1:18701. This separate Compose project does not copy fictional records into personal data. Keep real profiles out of test fixtures. External submission remains off.

## First useful walkthrough

1. In the fictional workspace, load the fictional demo and open a job.
2. Review the selected profile and its approved facts. Confirm that unapproved facts are not used.
3. Inspect Sources, job provenance, matching and the pipeline workbench.
4. Prepare a sample resume/cover letter and inspect document history.
5. Exercise a dry run against the local mock ATS; use fictional confirmation only.
6. Reload or restart the same workspace and inspect retained state.

The longer [user-testing guide](USER_TESTING.md) records capabilities and limitations. Missing controls or a failed step are defects to report, not permission to invent a successful result.

## Existing installations and upgrades

Before pulling changes, create and validate a backup following [LOCAL_BACKUP.md](runbooks/LOCAL_BACKUP.md). Preserve the original `.env` and credential-encryption key. A newly generated key cannot decrypt credentials encrypted with the old one.

From a clean source checkout, fetch updates and advance only by fast-forward, then rebuild with the same workspace mode and project. Do not discard local changes to obtain a clean update.

```powershell
git status --short --branch
git pull --ff-only
.\Start-MJobs.ps1 -Personal
```

Use the original startup mode instead of `-Personal` when updating the fictional workspace. Never switch modes against the same populated database. See [workspace identity and modes](runbooks/WORKSPACE_MODE.md).

## Troubleshooting

| Symptom | Safe response |
|---|---|
| Docker unavailable | Start Docker Desktop, select Linux containers and rerun startup. |
| Missing encryption key | Recover the original key/configuration; do not replace it against existing data. |
| Workspace mode mismatch | Read the mode runbook; retain the existing database. |
| Services not ready | Inspect Activity and the same project's Compose logs. Keep logs private if they contain applicant data. |
| Port occupied | Stop only your own conflicting instance or choose an unused port with the launcher's `-Port` option. |
| A browser journey fails | Record exact commit, steps and sanitized evidence in a bug report. |

Source builds are tested on Ubuntu CI; the user-facing launcher is PowerShell. Cross-platform installation promises beyond the executed acceptance evidence remain unverified.
