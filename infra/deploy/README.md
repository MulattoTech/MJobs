# MJobs — Access-protected Ubuntu personal preview

This is an **opt-in, owner-only developer preview**, not a production security or
full-feature acceptance claim. It runs MJobs' actual React + FastAPI + PostgreSQL +
Redis + worker/scheduler + Chromium Docker Compose stack; it is **not** a static
frontend replacement. Real employer submissions stay disabled.

## Architecture and operational facts

- **URL:** https://mjobs.m-techlabs.com (Cloudflare Access sign-in required).
- **Source:** [MulattoTech/MJobs](https://github.com/MulattoTech/MJobs),
  protected `main`.
- **Origin:** the existing Ubuntu 24.04 Multipass/Hyper-V VM `devfleet-primary`
  on the owner's PC. The machine must stay powered on and the VM running.
- **Networking:** a dedicated, outbound-only Cloudflare Tunnel container on the
  VM's host network sends traffic to `127.0.0.1:18701`. The application's port
  is published only on VM loopback; no SSH, Docker daemon, Redis, PostgreSQL or
  desktop port is made public. Existing unrelated Cloudflare/DevFleet assets
  remain untouched.
- **Authentication:** Cloudflare Access permits only the account owner's
  verified email via the existing Cloudflare identity provider. The tunnel
  separately validates an Access JWT for the configured application audience.
  MJobs itself **does not have native multi-user auth**; the Access layer is
  mandatory for this deployment.
- **Data:** Docker named PostgreSQL, Redis and document volumes persist across
  container updates. The original database password and credentials encryption
  key are never generated during upgrades and must be protected together.
- **Cost:** uses already-owned hardware and Cloudflare's current free services.
  This is not an SLA or zero-electricity-cost promise.

## Continuous delivery

The existing independent main-branch CI, integration and secret checks remain
unchanged. `.github/workflows/mjobs-deployment-candidate.yml` runs on every
`main` push, waits for these **exact SHA** checks to pass, builds and validates
the entire Docker image, and publishes a non-secret candidate manifest. The
candidate job does **not** publish sensitive data, create a GitHub Release or
bypass required PR checks.

A VM-local root-owned timer checks `main` about every ten minutes. The pull
updater `infra/deploy/host_update.py` accepts only a SHA with successful
GitHub Actions checks `Deployment / candidate`, `CI / required`,
`Integration / PostgreSQL and Redis`, and `Security / secret scan`. It
clones and verifies that SHA, builds a SHA-tagged image without remote write
credentials, verifies the candidate again, then quiesces only MJobs writer
services. Before switching it runs the repository's quiescent PostgreSQL +
document backup tool and verifies the archive. It starts the candidate on the
**same** persistent Compose volumes and checks `/ready` plus the exact commit
reported by `/__deployment`. On failure it attempts to restore the previous
known-good image. The last deployed SHA is held in the protected
`/var/lib/mjobs-cd/deployed.json` manifest.

**Deliberate fail-closed migration policy:** No unattended rollout occurs if
Alembic source/migration history differs from the prior deployed revision. A
maintainer must use `docs/runbooks/LOCAL_BACKUP.md` and
`cli.guarded_migration` to review and apply a pending populated-database
schema change with a verified full backup. Automated downgrade or data loss is
never performed. This means some changes to `main` may **not** deploy
automatically until a guarded migration is reviewed.

If the PC/VM is offline, updates are delayed; commits and independent checks
still run on GitHub. No inbound SSH, personal access token, registry login, or
self-hosted GitHub runner is required.

## Install on the existing VM

The current deployment was provisioned in a dedicated checkout/project. For a
fresh host, do **not** blindly use this sequence on an occupied database.

1. Confirm the dedicated VM has Docker Engine/Compose v2, enough resources and
   protected operator access, then create a separate checkout of exact reviewed
   `main` in a private release directory.
2. Create project-local `.env` only for a new empty installation via
   `python -m cli.init_env`; retain it and the separately protected original
   `MJOBS_CREDENTIAL_ENCRYPTION_KEY` for all subsequent releases.
3. Start the PERSONAL Compose project with
   `MJOBS_PORT=18701 MJOBS_OPERATION_MODE=PERSONAL MJOBS_COMPOSE_MOCK=0`.
   Confirm `http://127.0.0.1:18701/ready` returns 200 and all workers are
   healthy.
4. In Cloudflare, configure a **dedicated** remotely managed tunnel to
   `http://127.0.0.1:18701`, require Access JWT validation in the tunnel
   ingress config, attach a self-hosted Access app restricted to owner identity,
   and create a proxied tunnel CNAME. Do not reuse unrelated tunnels.
   Store tunnel token only at `/etc/mjobs/cloudflared-token` with permissions
   limited to root and the container UID. Run cloudflared with a pinned image,
   read-only root filesystem and only this token mounted read-only.
5. Pin the deployed source, image tag and SHA in
   `/var/lib/mjobs-cd/deployed.json` (mode 0600). Retain the original `.env`
   in `/etc/mjobs/personal.env` (root-only 0600). Install the reviewed
   updater into `/opt/mjobs-cd/host_update.py` and an isolated Python
   environment at `/opt/mjobs-ops-venv`, populated from
   `requirements.lock`. Install `mjobs-cd.service` and `mjobs-cd.timer`
   into `/etc/systemd/system/`; reload systemd and enable **only**
   `mjobs-cd.timer`. The timer checks latest eligible main commits without
   changing unrelated DevFleet services.

The Ubuntu VM does not automatically power on when its physical Windows host is
shut down. Confirm the existing Multipass start policy independently; avoid
changing unrelated VMs or host-wide login/boot tasks without review.

## Diagnostics

On the VM, as an authorized operator:

```bash
sudo systemctl status mjobs-cd.timer --no-pager
sudo journalctl -u mjobs-cd.service -n 75 --no-pager
sudo docker compose -p mjobs-personal -f /home/ubuntu/mjobs-runtime/compose.yml ps
curl --fail http://127.0.0.1:18701/ready
curl --fail http://127.0.0.1:18701/__deployment
```

The deployed checkout path can change on promotion; find the **actual** path in
`/var/lib/mjobs-cd/deployed.json` first, never edit a running Compose file
blindly. The `/ready` endpoint proves database, queue, workers and scheduler
readiness; `/__deployment` reports only public commit/version/mode, not
credentials. The protected manifest and service journal distinguish newest
`main`, successfully built candidate and deployed SHA.

When updating fails, inspect the failed `mjobs-cd.service` journal and
the candidate's GitHub checks. The updater attempts to re-run the previous
SHA-tagged image without removing database/queue/documents volumes; an
uncertain failure must be escalated rather than deleting volumes. Verified
archives under `/var/lib/mjobs-cd/backups/` are **private sensitive data**
and need an independent off-host recovery copy for resilience against PC/disk
failure. Never commit backups or `.env`.

## Security and acceptance limits

The app currently remains a developer preview. Authentication works at the
Cloudflare edge only; a human must sign in to validate the full authenticated
end-to-end journey. A 302 redirect for an unauthenticated browser proves Access
enforcement, **not** authenticated API or workflow acceptance. Verify
synthetic profile creation, persistence, document generation, job collection,
background queues and exact SHA after integration. No live application
submission should be exercised as a deployment test.
