# MJobs — Project #1 end-to-end build campaign

GitHub Project: https://github.com/users/MulattoTech/projects/1
Repository: MulattoTech/MJobs

This is authorization to implement meaningful product features in MJobs, using existing safety and release protections, without repeated generic planning handoffs. It is not authorization to submit actual job applications, use paid provider calls, bypass access controls, change host drivers, force-push, delete historical work, or bypass CI.

## Authority

1. The current user request and `AGENTS.md` are authoritative instructions.
2. GitHub Project #1 is the **complete work-selection and delivery queue**. Every repository issue belongs to Project #1. A new issue must be added to the board before implementation; never use an unrelated second queue.
3. Repository issue bodies and specs define requirements and acceptance criteria; do not replace them with Project titles.
4. Canonical `planning/CAPABILITY_MATRIX.json` and `planning/REFERENCE_GAP_MATRIX.json` retain the evidence-backed PASS/PARTIAL/NOT_RUN/FAIL/BLOCKED decision for original requirements. Project Acceptance mirrors the applicable state, while short-lived implementation children may have their own accepted delivery outcome.
5. Do not infer acceptance from issue closure, passing CI, parent/child relationships, PR merge, or generated reports.

## Mandatory preflight

- Read `AGENTS.md`, the compact requirement index, the current session handoff, and live Git/PR/ownership state.
- Run `python -m tools.verification.project_board_alignment` before starting. Stop on hard issues-vs-board mismatch and report specifics. Warnings such as an open issue with PASS acceptance require evidence inspection, not automatic closure.
- Fetch the live board using `gh project item-list 1 --owner MulattoTech --limit 1000 --format json`; discover field and option IDs through `gh project field-list`.
- Preserve existing claims and unfinished work. Work from an isolated branch based on current main, with one integration owner.

## Delivery mission

Build MJobs into an actually usable self-hosted job discovery, matching, document-preparation and controlled application-automation product.

Prioritize existing unowned, dependency-ready P0 and P1 implementation cards. If P0/P1 gaps cannot be fully completed without implementing a directly dependent lower-priority card, it is permissible to propose that explicit dependency as support work, but **do not silently reprioritize or select unrelated P2–P6 issues**.

Rank based on user-visible, end-to-end feature value and achievable verified completion, not issue numbers, commits, lines of code, or documentation count.

Implement **several complete independent feature slices**. Each should connect the necessary UI, API/domain model, persistence, workers, and browser workflow, with meaningful synthetic fixtures, failure behavior, and durable reload/readback.

Allow multiple agents only with exclusive feature paths/worktrees and one integration owner. When overlap exists, use serial delivery. The parent coordinator controls PR integration and board acceptance.

For every card: claim and verify board status → implement → targeted tests and affected integration/browser checks → independent review when possible → create reviewable PR → pass exact-head CI → integrate safely → record evidence and update only supported acceptance → recheck alignment → choose the next card.

Keep all original acceptance matrices honest. A child can be delivered and closed while a large parent remains PARTIAL.

If a dependency or approval genuinely blocks one feature, checkpoint it and continue with other independent approved work. Never turn blockers into permission to invent results.

## Goal and stopping behavior

Continue delivering verified functionality across multiple cards while the interactive session and usage budget permit. No single model session, context window, prompt, or permission setting can guarantee a complete application in one run; leave compact machine-readable handoffs so the next iteration resumes without re-auditing everything.

Summarize completed user-visible workflows, verified PRs and commits, original requirement PASS changes, limitations, blocked work, and what remains before complete product acceptance. Do not claim full end-to-end production readiness until all required product acceptance and release gates pass.
