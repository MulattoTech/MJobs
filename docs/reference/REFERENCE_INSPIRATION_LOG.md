# Reference inspiration log

This log records cited design concepts and the corresponding MJobs slice. A
reference citation is not evidence of code reuse or permission to reuse code.
The registry pins repository and license evidence separately.

| MJobs issue | Source project and pinned ref | Material concept cited | MJobs implementation record |
|---|---|---|---|
| #373 / MJ-REF-003 | CareerWeaver, `idea-torx/CareerWeaver`, `34068a1efe8eb8fcf33c51b5e9fa42d1205cd72d` (`src/weaver/guardrail.py`), MIT | Enforce a configured avoid-list against generated career content | `docs/evidence/2026-10-06-avoid-list.json`; MJobs issue/evidence defines whole-term matching, output validation, privacy-safe errors, and historical document checks. No CareerWeaver source file is included in MJobs. |
| #374 / MJ-REF-004 | CareerWeaver, `idea-torx/CareerWeaver`, `34068a1efe8eb8fcf33c51b5e9fa42d1205cd72d` (`src/weaver/preflight.py`), MIT | Check required ATS questions and profile coverage before browser work | `docs/evidence/2026-10-06-ats-question-preflight.json`; MJobs uses its own typed mappings, local mock metadata contract, persistent audit rows, and application states. No CareerWeaver source file is included in MJobs. |
| #384 / MJ-REF-014 | AutoApply, `Liam-Frost/AutoApply`, `47d9e8b04d8d213bda6a9422b08edf3a75279e5a` (`profile_lookup` concept), PolyForm Noncommercial 1.0.0; ResuMorph, `hannibalevit/resumorph`, `7ac37b3769a2550f730bb00cf8441826fdb706e0`, Apache-2.0 | Minimize task context and audit which approved profile facts are accessed | `docs/evidence/2026-10-06-least-data-profile-access.json`; MJobs allowlists its own fixed tasks/paths and value-free audits. No source or assets from these projects are included in MJobs. AutoApply remains inspiration-only. |
| #387 / MJ-REF-017 | AutoApply, `Liam-Frost/AutoApply`, `47d9e8b04d8d213bda6a9422b08edf3a75279e5a`, PolyForm Noncommercial 1.0.0; Personal JobPilot, `TheCromazone/Personal-JobPilot`, `a582c29d0f1aed97b93b13d843671487ab8c25d3`, MIT | Atomic artifact writes, quarantine/restore, and safe cache publication | `docs/evidence/2026-10-06-artifact-recovery.json`; MJobs uses its own artifact inventory, manifests, operations, and tests. No source or assets from these projects are included in MJobs. AutoApply remains inspiration-only. |
| #398 / MJ-REF-028 | ResuMorph, `hannibalevit/resumorph`, `7ac37b3769a2550f730bb00cf8441826fdb706e0` (`server/app/security.py`, `server/app/routers/settings.py`, `extension/src/sidepanel/SettingsView.tsx`), Apache-2.0 | Keep provider secrets in server-side encrypted storage and expose masked client metadata | `docs/evidence/2026-10-07-provider-credential-store.json`; MJobs independently uses AES-GCM with profile/provider associated data, a separate server environment key, fixed-origin connection probes, and masked-only responses. No ResuMorph source, tests, prompts, prose, or assets are included in MJobs. |
| #386 / MJ-REF-016 | AutoApply, `Liam-Frost/AutoApply`, `47d9e8b04d8d213bda6a9422b08edf3a75279e5a`, PolyForm Noncommercial 1.0.0 | Durable operator-visible failure queue with explicit retry/discard decisions | `docs/evidence/2026-10-07-dead-letter-queue.json`; MJobs independently persists operation timestamps, cumulative attempts, stable idempotency identity, append-only audit events, and profile-scoped operator actions. Ambiguous submissions remain reconciliation-only. No AutoApply code, tests, prose, or assets are included. |

The issue contracts and linked MJobs evidence are the source for these historical
design references. This log does not assert that every historical source tree was
independently audited for code similarity. New work must complete the pre-merge
checklist before integration.
