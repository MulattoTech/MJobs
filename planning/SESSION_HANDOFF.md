# MJobs implementation checkpoint — 2026-10-11

Canonical root `C:\Tools\Dev\MJobs`, branch `main`; baseline local/remote `22fd8b765a7d9027aa00043a53fbf87869c7b1b4`. Use live Git for the current checkpoint SHA. Historical worktrees and personal stack are preserved.

Implemented: atomic startup credentials and fail-closed legacy key handling; persistent bounded discovery retry deadlines, source cooldowns and attempt fencing; shared provider errors; fact-preview/navigation races; HTTP upload integrity; complete backup writer quiescence. See `docs/evidence/2026-10-11-durable-discovery-recovery.json` for exact source hashes and limits. Parent requirements remain PARTIAL.

Verified: 44 PostgreSQL integration tests + 12 subtests; 78 source/retry tests + 19 subtests; 10 browser regressions; 766 frontend tests/build; actual isolated launcher; connected mock browser journey with confirmation count one; Redis outage/outbox recovery/full-service restart; separate-target backup/restore with 306 files and identical database inventories/document hashes. Broad Python rerun is still running with two failures observed: resolve diagnostics before publication. Hosted checks NOT_RUN. Real external submission OFF.

Counts unchanged: originals 102 PASS / 109 PARTIAL / 155 NOT_RUN (366); P0 7/8/11; P1 35/31/50. References 19 PASS / 4 PARTIAL / 36 NOT_RUN (59). No full RC or full acceptance claim.

Owned synthetic stacks: `mjobs-rc-20261011` UI18810/PostgreSQL18811 and fresh restore `mjobs-restore-20261011` UI18812. Private ignored configuration/helpers/evidence in `outputs/rc-20261011/`. Root `.env` and existing personal containers are untouched; legacy root config lacks the original encryption key and remains fail-closed. Do not replace its key or touch personal volumes. Tested launcher: `.venv/Scripts/python.exe outputs/rc-20261011/run.py pwsh -NoProfile -NonInteractive -File Start-MJobs.ps1 -NoBuild -NoOpen -Project mjobs-rc-20261011 -Port 18810`.

User now explicitly requests six teams of three Luna High agents. Root remains sole integrator; new isolated worktrees under `.worktrees/rc-team-*` only, no standing lane infrastructure. Teams own posting structure, aggregator enrollment/provenance, credential-aware restore, ATS question review, supported recovery utility, truthful verification. Read-only investigators/reviewers do not mutate shared files. Integrate validated commits serially, update this checkpoint and evidence before safe non-force publication.

Highest next product gap: public posting HTML is flattened, leaving responsibility context unavailable and blocking truthful live-source preparation. Public Greenhouse Tailscale read-only collection returned63 jobs; browser readback passed but preparation safely blocked. Team1 will repair structure with deterministic provenance/truthfulness tests. No actual external applications authorized.
