# Bootstrap and governance implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Preserve current GitHub history, establish the durable control plane and issue catalogue, and implement the first executable safety slice.

**Architecture:** Keep the supplied scoped specifications and queue authoritative. The first PR adds the control plane and tested safety policy used by later application workers. Requirement completion remains separate from individual test results.

**Tech Stack:** Python 3.12+, Git, GitHub CLI; project-local dependencies only.

**Spec:** AGENTS.md, CODEX_START.md, docs/spec/areas/governance_safety.md.

## Global constraints

- External auto-submit is OFF by default; real submission requires explicit user enablement plus product gates.
- Never fabricate career facts, qualifications, answers, credentials, demographic data, or PASS evidence.
- Never bypass CAPTCHA, MFA, access controls, anti-bot protections, or rate limits.
- main must remain releasable; never discard unrelated changes or force-push.
- No GPU requirement, unrelated host modifications, or host reboot.

## Review focus

- Interrupted issue creation must preserve its last acknowledged map and recover from remote markers.
- Ambiguous issue-create responses must not trigger a blind duplicate POST.
- User enablement alone must not bypass application quality, limits, or challenge gates.
- Unverified facts must never enter a prepared document.
- Ambiguous submission must retain SUBMISSION_UNCONFIRMED across retry attempts.

## Task 1: Preserve and bootstrap

Files: supplied control pack, .gitignore, docs/decisions/0001-execution-and-preservation.md, planning/IMPLEMENTATION_STATE.json.

- [ ] Reconcile local files and GitHub main/issues/PRs; retain newer README/history.
- [ ] Verify catalogue IDs, queue dependency references, and supplied hashes.
- [ ] Commit the allowlisted control pack on the governance branch and push it.

## Task 2: Materialize and harden the backlog

Files: bootstrap-tools/sync_backlog.py, tests/unit/test_sync_backlog.py, planning/GITHUB_ISSUE_MAP.json.

Interface: stable hidden requirement markers map requirement IDs to issue numbers.

- [ ] Execute python bootstrap-tools/sync_backlog.py --repo MulattoTech/MJobs and record its result.
- [ ] If an operational defect occurs, write failing behavioral tests for timeout, checkpoint/resume, duplicate markers, and ambiguous create recovery before repairing it.
- [ ] Respect GitHub rate limits; stop on a classified external restriction and retain partial evidence.
- [ ] Reconcile the completed or partial map against live remote markers and commit it.

## Task 3: Executable safety policy

Files: services/security/AREA.md, services/security/policy.py, tests/unit/test_safety_policy.py.

Interfaces: evaluate_submission(context, policy) returns an explicit allowed/blocked decision; select_verified_facts(profile, requested_ids) returns only verified profile facts or raises a validation error.

- [ ] Write tests for external-disabled default, intervention barriers, factual provenance, limits/quality/duplicate gates, and SUBMISSION_UNCONFIRMED retry prevention.
- [ ] Run python -m unittest discover -s tests -p 'test_*.py' and observe the missing behavior fail.
- [ ] Implement the smallest deterministic policy; mock-site browser work remains a later coherent slice unless dependency-unblocked.
- [ ] Run the full suite and independent read-only review; repair substantive findings with regression tests.

## Task 4: Evidence and integration boundary

Files: docs/evidence/, planning/IMPLEMENTATION_STATE.json, planning/PR_QUEUE.json, PR description.

- [ ] Record timestamp, command, target, result, evidence path, and unimplemented requirements.
- [ ] Commit/push a real implementation and open a draft PR linked to mapped issues.
- [ ] Verify remote branch, checks, diff safety, and local status before marking ready.
- [ ] Continue dependency-unblocked work; request integration authorization only for a concrete reviewed PR when necessary.
