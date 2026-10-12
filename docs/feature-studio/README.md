# Feature Development Studio — phased rollout and operator guide

## Scope and current truth

This repository contains a **safe local pilot** for the request to introduce a guided,
feature-driven development workflow. The submitted `Test Feature` is intentionally a
non-production smoke test, **not** a P0 MJobs product outage, and is not part of the
366 + 59 product acceptance catalogues. Its priority is stored as supplied for testing.
Do not treat its schema-validation success as proof of a product feature being delivered.

**Implemented in this pilot**
- A versioned, machine-readable schema in `feature-studio/schema.v1.json`.
- The actual intake and risk/acceptance metadata in `specs/FS-TEST-001.json`.
- A browser GUI at Mission Control's `/static/feature-studio.html` (local/offline
  draft editing, deterministic follow-up prompts, import, and JSON export).
- A dependency-free CLI validator and unit regression tests, executed by existing CI.
- A pilot configuration with GitHub write automation, agent execution, and
  account-wide rollout **disabled** until independently authorized.

**Not implemented**: live agent-led repository probing, GitHub App, multi-repo
catalogue, GitHub Projects API writeback, branch/PR creation through this GUI,
credential management, approval service, or agent job scheduler. These require
separate implementation and security review. The questionnaire's prompts are rules,
not claims of an AI model inspecting the codebase.

## Smoke-test walkthrough

1. Start MJobs Mission Control from the reviewed local MJobs checkout.
   Open `http://127.0.0.1:18704/static/feature-studio.html` (adjust port if
   Mission Control uses another port). Or open the HTML file directly for offline drafting.
2. Enter or edit feature inputs; change the scope to AI or infrastructure to
   see the corresponding deterministic architecture questions.
3. Download a JSON draft. From repository root, run:

   ```powershell
   python -m tools.feature_studio.validate --all
   python -m tools.feature_studio.validate --spec <path-to-exported-JSON>
   python -m pytest -q tests/unit/test_feature_studio.py
   ```

4. Confirm that the fixture has `lifecycle=DRAFT` and every acceptance
   criterion is `NOT_RUN`. Tests of validation and of the browser workflow must
   be independently evidenced **on the same revision** before advancing status.
5. Have a human review the specification, dependencies, risk and tests.
   Only after approval may an isolated implementation workstream be planned.
   No production submissions or GitHub mutations originate from this GUI.

## Spec → implementation → acceptance

1. **Draft**: record a problem, user-visible outcome, specific acceptance
   criteria, constraints, dependencies, owner/repository, priority and risk.
2. **Probe**: repository-aware agent *proposes* affected paths, API contracts,
   tests and blocking decisions with source references. Require confirmation.
3. **Approve**: authorized human approves *specific spec revision*. Store
   reviewer identity, digest, UTC timestamp and decision in an auditable service.
   The pilot has no such service, so approval flags must remain false.
4. **Decompose**: derive coherent vertical slices and workstream ownership.
   Conflicting file/API scopes are serialized. Independent worktrees only.
5. **Implement**: use short-lived PRs. Respect existing MJobs `AGENTS.md`,
   release gates and requirements; never force push or change dirty worktrees.
6. **Verify**: record exact commit SHA, CLI commands, CI run URLs, test outputs,
   reviewer decision, failures and recovery notes. `PASS` requires actual
   criterion-specific evidence, not only the JSON's status label.
7. **Merge**: human review + required branch rules/checks, then update
   spec traceability and project tracking. Partial features remain PARTIAL.

## Roadmap (dependency ordered)

| Phase | Scope | Measurable exit gate |
| --- | --- | --- |
| 0 — pilot (this PR) | Schema, draft fixture, GUI, validator, tests, docs | Validator + tests green; reviewable PR; no agent/organization writes |
| 1 — live MJobs intake | MJobs-local authenticated API and protected versioned spec storage; idempotent PR-based spec sync | Integration tests for malformed input, duplicate submissions, auth and retries; reviewer approval |
| 2 — GitHub App | Least-privilege installation, webhooks signature verification, permission scoping, retry queue and audit log | End-to-end tests on disposable test repository; denied permissions fail closed |
| 3 — agent-led probing | Allowlisted read-only repository discovery, cited affected-path analysis, adaptive probing and dependency graph | Synthetic-repo tests prove no fabricated dependencies, code execution or data leakage |
| 4 — controlled execution | Isolated worktree scheduler, conflict detection, policy-checked agent jobs, human PR reviews, immutable evidence | Unapproved execution and merge are demonstrably rejected |
| 5 — account-wide rollout | Opt-in repository selection, reusable templates/rulesets, projects sync, drift detection, per-repo overrides | Canary repos pass contract tests, rollback/drift reconciliation verified |

These stages are **not automatically authorized** by the requested `Coverage: all`
field. The repository owner is a GitHub *user account*, `MulattoTech`, not
necessarily a GitHub organization. GitHub App installations and Projects access
must be confirmed for that owner, along with repository selection.

## Multi-repository control plane

Preferred long-term architecture: separate **Feature Studio** service, not a
privileged extension inside MJobs product runtime.

- **Frontend**: schema-backed wizard, conditional probing questions, specs
  comparison, provenance, approval queue and dry-run change preview.
- **API/database**: authenticated owner/repo membership, versioned specification
  records, approvals, request idempotency keys, minimal audit events, secrets
  isolation and transactional outbox.
- **GitHub App**: installation-scoped least-privilege `Contents: read`,
  `Metadata: read`, `Pull requests: read/write` and `Contents: write`
  only when spec-PR sync is enabled. Issues, Actions/Checks and Projects
  permissions are optional separate scopes for enabled modules. Verify actual
  GitHub permission requirements before installation; keep tokens server-side.
- **Webhooks**: signature validation, replay defense, delivery-id deduplication,
  queued retries, backoff/rate-limit handling and full audit trail.
- **Agent workers**: read-only probing by default; optionally authorized
  branch+PR writes in ephemeral sandboxes, with patch review, allowlisted
  commands, per-repo budgets and cancellation. Never autonomous main push.
- **Templates/policies**: opt-in installs using config PRs, templates and
  existing GitHub rulesets; never overwrite repository-specific protections.
- **Projects sync**: derived state only, not acceptance authority; project
  metadata and spec evidence must reconcile even if GitHub webhooks arrive late.

Multi-repo deployment needs an installation approval flow, hosting, persistence,
GitHub App registration, documented threat model, migration/backup procedures,
canary rollout, on-call ownership and a rollback mechanism. The pilot has
none of those credentials or broad authorizations.

## Safety and evidence

- Local GUI is an **offline exporter**; do not paste credentials, applicant
  information or sensitive business data into drafts that will become public.
- Browser import only copies editable fields. Claims of PASS, APPROVED or agent
  permission in imported JSON are deliberately discarded.
- The CLI validates spec **structure and basic lifecycle invariants** only.
  It cannot attest test execution, evidence authenticity, review identity or
  GitHub branch protections.
- `feature-studio/config.json` intentionally fails validation if organization
  rollout or write/execute flags are enabled prematurely.
- Preserve existing MJobs CI. Tests in `tests/unit` run in the fast Python job;
  required-check enforcement still depends on GitHub repository branch rules.
- Retain human merge approval. Maintain signed approvals and revision-bound
  evidence when adding API-backed lifecycle transitions in future phases.

## Phase 1–5 integration update (October 12, 2026)

A separate private account-wide controller now lives in
[`MulattoTech/FeatureStudio`](https://github.com/MulattoTech/FeatureStudio).
MJobs retains the existing Mission Control entry point and adds:
- local SQLite-backed draft records with optimistic revision checks,
  idempotency keys and audit events;
- read-only multi-repository inventory through an authenticated GitHub CLI;
- tracked-file probing of MJobs and isolated disposable shallow-clone probing
  for other authorized repositories, producing explicit non-executing work plans;
- optional CLI for *draft-PR-only* GitHub issue form rollout.

### Truth in status

The MJobs service does not register an installation-scoped GitHub App or
autonomously launch multiple coding models from the Feature Studio GUI.
There is no unattended agent execution; existing Mission Control
review-gated launch remains the only supported code-execution integration.
The account-wide service maintains its own local persistence and GUI.

Run `python -m pytest -q tests/unit/test_feature_studio*.py` and
`python -m tools.feature_studio rollout-preview`. The rollout preview
never edits repositories. CLI `install-form --repo ... --apply` can create
a draft PR in a selected repository; it refuses to overwrite an existing
form and never merges. Existing MRaiders Feature Control has a custom
workflow and must be integrated via its own contract, not overwritten.

Security requirements for future production rollout include GitHub App
registration/consent, signed authorization records, webhook replay handling,
independent agent execution budgets, Projects reconciliation, and accepted
repository-specific CI gates. Those must be verified before marking phases
2–5 production-complete.
