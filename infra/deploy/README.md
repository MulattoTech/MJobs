# MJobs — Windows Docker Desktop owner-only preview

This is a developer-preview deployment, **not** a production-readiness claim. It
serves the **real full stack**: React, FastAPI, PostgreSQL 17, Redis 7,
scheduler, discovery/AI/document/application workers and Playwright Chromium.
Real employer submissions remain OFF. There is no native MJobs multiuser auth.

## Live architecture

- URL: **https://mjobs.m-techlabs.com**, restricted to the owner's Cloudflare
  account email using the Cloudflare Access application `MJobs Personal Preview`.
- Origin: existing Windows PC `MulattoTechBox`, running Docker Desktop's
  **shared WSL2 Linux engine**. No standalone 8 GB Hyper-V VM is needed for MJobs.
- Compose source: `C:\Tools\Dev\MJobs-Micro` — an **isolated clone**, not
  the active `C:\Tools\Dev\MJobs` developer checkout.
- Compose project: `mjobs-micro`. Local API/frontend:
  `http://127.0.0.1:18801` only (never LAN-exposed).
- Small dedicated network override: `compose.micro.yml` copied from
  [infra/deploy/windows/compose.micro.yml](windows/compose.micro.yml).
  It uses the unused `192.168.64.0/27` source-egress subnet because the PC's
  Docker Desktop automatic address pools are full. Re-evaluate for other hosts.
- Cloudflare Tunnel: `mjobs-windows-docker`. A separate hardened
  `mjobs-micro-cloudflared` Docker container joins the `mjobs-micro_ui`
  bridge and forwards to `http://api:8000`. The Cloudflare Tunnel ingress
  separately requires and validates the Access JWT audience. Its token is a
  protected local file and never belongs in Git or Compose environment output.
- The old Ubuntu VM `devfleet-primary` is **no longer the MJobs origin**.
  Its MJobs Compose services and tunnel are stopped and its update timer is
  disabled; its original volumes and verified backup remain intact. The VM's
  unrelated DevFleet control-plane service remains running and must not be
  terminated as part of MJobs operations.

The Windows Docker environment is already shared with the owner's other
services. It does not reserve the Ubuntu VM's 8 GB for MJobs. At migration the
ten MJobs runtime containers together consumed roughly 0.8 GB of active memory;
Docker/WSL page cache can temporarily grow considerably during image builds.
Docker's WSL2 backend can reclaim unused cache over time. Windows PC and
Docker Desktop must stay running for the site to stay available.

## Startup and resilience

The Docker Compose services and the Cloudflare Tunnel sidecar have
`restart: unless-stopped`; Docker Desktop itself was configured to start
silently at **Windows user sign-in** by the per-user Startup helper
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MJobsMicro-StartDocker.vbs`.
There is **no** unattended boot guarantee before Windows user sign-in, no
outside-network port forwarding, and no public Docker daemon/Redis/PostgreSQL.

Secrets remain in `C:\Tools\Dev\MJobs-Micro\.env` (NTFS ACL owner, SYSTEM
and Administrators only). A new key was generated for the new **empty**
workspace: no career profiles, applications, documents, job listings, or
provider credentials existed on the original VM. The original VM's keys,
volumes and backups were preserved; they were not copied or overwritten.

Check services from a Windows PowerShell terminal without printing secrets:

```powershell
docker ps --filter label=com.docker.compose.project=mjobs-micro
docker ps --filter name=mjobs-micro-cloudflared
Invoke-RestMethod http://127.0.0.1:18801/ready
Invoke-RestMethod http://127.0.0.1:18801/__deployment
docker logs mjobs-micro-cloudflared --tail 40
```

The local `/__deployment` endpoint exposes only the source SHA, version and
workspace mode. A 302 to Cloudflare Access proves unauthenticated protection;
the owner must complete an actual Access login to verify the browser session.
The local API, frontend and worker readiness were verified independently.

The new Windows baseline backup was created and verified using
`python -m cli.local_backup`, then the application was restarted and checked
again. The protected backup is under
`%LOCALAPPDATA%\MJobsMicro\backups`. An off-PC recovery copy remains
necessary for resilience to hardware/disk failure. Never use
`docker compose down -v` to troubleshoot a populated project.

## Verified-main deployment without always-on agents

The existing GitHub main workflows still run independent CI, integration
and security checks. `.github/workflows/mjobs-deployment-candidate.yml`
builds the full candidate from each main SHA only after those required
checks pass. It produces a non-secret candidate manifest; it does not
publish personal data or call an untrusted self-hosted runner.

The per-user Windows Scheduled Task **MJobs Micro - Verified Main Update**
runs silently via `pythonw.exe` every ten minutes; it has no resident
controller. Its source is
[windows/update_mjobs_micro.py](windows/update_mjobs_micro.py), installed
as `%LOCALAPPDATA%\MJobsMicro\update.py`. It reads the protected
`%LOCALAPPDATA%\MJobsMicro\state.json` baseline and only considers an
exact main commit after these checks all report success:

- Deployment / candidate
- CI / required
- Integration / PostgreSQL and Redis
- Security / secret scan

A verified commit is cloned to an isolated release directory, rechecked
against latest main, compared with the currently deployed Alembic migration
sources, and built into a SHA-tagged Docker image. **Changed migration
sources block unattended rollout.** A guarded operator-approved migration
is necessary; no automatic database downgrade is attempted.

An interrupted release directory can be reused only after verifying its exact
GitHub origin and commit, independent Git metadata, unchanged tracked files,
and plain filesystem paths. Only the protected `.env` and `compose.micro.yml`
overlays may be extra files, including ignored files. Unknown or modified
candidates are retained and blocked for reconciliation. All paginated GitHub
check runs are verified against the exact commit; a newer queued run blocks an
older successful run with the same required check name.

For an eligible unchanged-schema build, the updater quiesces only the
`mjobs-micro` application writers, uses MJobs' own local backup utility to
save/verify PostgreSQL and document data, starts the new image with the
**same persistent volumes**, then requires `/ready` and `/__deployment`
to report the expected exact SHA. A failure tries to restore the prior
known-good image. The updater never edits active worktrees, changes unrelated
Docker containers or host network/driver settings, or submits a real job
application.

Inspect the private updater journal without revealing secrets:

```powershell
Get-Content "$env:LOCALAPPDATA\MJobsMicro\update.log" -Tail 40
schtasks.exe /Query /TN "MJobs Micro - Verified Main Update" /V /FO LIST
```

If a new `main` revision has not passed all checks, **the current MJobs
version is kept running**. Read `/__deployment` for the exact running revision
and the private `MJobsMicro/state.json` for its verified release directory.
Do not treat a successful pull request or static README page as full production
acceptance.

## Rollback and limitations

Manual fallback: keep both old SHA image and original isolated checkout;
use the known `mjobs-micro` Compose project with its original image tag,
environment mode, port and verified backup. Do not clear named volumes or
regenerate the credential encryption key. If an unknown application
submission is encountered, retain the durable state for human review;
never blindly replay.

The old Ubuntu MJobs deployment is retained only as an extra recovery
reference. It must not be restarted in parallel with the Windows PERSONAL
stack or reattached to the public DNS hostname.

Deployment evidence must separately establish authenticated Cloudflare access,
exact running source, and application readiness. Remote personal-document
workflows and an automated rollback drill require their own recorded proof.
The MJobs product remains a developer preview with unsatisfied acceptance
criteria; publishing a new build does not complete those criteria.
