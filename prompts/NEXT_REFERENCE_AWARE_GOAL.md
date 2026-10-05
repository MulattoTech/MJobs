> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Next Codex Goal — Reference-Aware MJobs Completion

`/goal Reconcile the current MJobs stacked PR chain, original 366-item requirement catalogue, and canonical MJ-REF-001..042 reference-gap backlog. Preserve main and all newer work. Resolve the current CI/workflow authorization blocker when the environment permits, then continue implementation in dependency order using the smallest coherent vertical slices. Original P0-P3 correctness, truthfulness, persistence, identity/deduplication, recovery, company-discovery and safe application requirements outrank optional reference features. Never open empty PRs, never exceed the repository active-PR policy without proving file ownership is non-overlapping, and never claim PASS without executed evidence. For each selected slice: read AGENTS.md, planning/IMPLEMENTATION_STATE.json, planning/PR_QUEUE.json, planning/REFERENCE_GAP_MATRIX.json, docs/FEATURE_COMPLETENESS.md, the active GitHub issue(s), and only the relevant area spec/code. Implement, test narrowly, run affected integration/E2E, record evidence, update both original/reference status surfaces, commit, push, and create/update the draft PR. Continue until no dependency-eligible work remains or a genuine external/environment blocker requires user action. Do not perform real external job submission without explicit user enablement and normal product gates.`

## Execution policy
1. Re-fetch GitHub before choosing work. Another agent/session may have advanced branches, issues, or PRs.
2. Treat the current stacked implementation chain as evidence, not as automatically merged truth.
3. Integrate or rebase only when safe; never force-push and never discard unrelated work.
4. First resolve blockers preventing verified work from reaching the repository or CI.
5. Work original PR_QUEUE dependencies before lower-priority convenience features.
6. Interleave MJ-REF issues only when their dependency foundation is already stable or when the gap directly strengthens P0-P3 safety/durability.
7. The canonical reference-gap namespace is MJ-REF-001..042. Do not recreate closed duplicate REF-GAP issues.
8. Use docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md for inspiration intent and prohibited patterns.
9. Source projects are references, not dependency mandates. Reimplement behavior independently unless license compatibility is explicitly established.
10. Never import JobSpy-style block-bypass behavior or AIHawk-style stealth/evasion into MJobs.
11. Keep external auto-submit OFF by default and preserve SUBMISSION_UNCONFIRMED semantics.
12. Preserve verified-fact provenance through resume, cover-letter, answer, interview-prep, outreach, and browser-assist surfaces.

## PR selection order
### Gate A — integrate current verified spine
- Reconcile #367 -> #368 -> #369 -> #370 and their remote CI status.
- Do not merge blindly; verify each head, required checks, and stacked dependency state.

### Gate B — original product foundation
- Continue dependency-eligible items in planning/PR_QUEUE.json.
- Prioritize identity/dedupe, company/ATS discovery, durable operations, career facts, matching, documents, and application safety.
### Gate C — reference safety/durability improvements
Prefer these when their dependencies are ready:
- MJ-REF-003 avoid-list enforcement
- MJ-REF-004 ATS question preflight
- MJ-REF-005 deterministic no-LLM fallback
- MJ-REF-010 claim-level mutation guard
- MJ-REF-011 AI budgets/truncation
- MJ-REF-013 bounded agent ToolRegistry
- MJ-REF-014 least-data AI profile access
- MJ-REF-016 dead-letter queue
- MJ-REF-017 artifact quarantine/atomic writes
- MJ-REF-028 credential store
- MJ-REF-030 coverage-floor CI
- MJ-REF-032 least-privilege browser bridge
- MJ-REF-033 run stopping conditions
- MJ-REF-037 license/provenance policy
- MJ-REF-040 machine-readable agent contract
- MJ-REF-042 privacy/data-flow transparency

### Gate D — reference product enrichment
Then schedule role lenses/story memory, document workbench, interview prep, multilingual support,
browser companion, company advisor, outreach/upskilling, source breadth, scoring profiles,
multi-JD comparison, and bounded document quality feedback.
## Evidence and completion
- Original MJ-REQ completion and MJ-REF completion are separate matrices.
- Only PASS contributes to completed percentage.
- PARTIAL stays visible as PARTIAL.
- Release readiness is a separate gate.
- Every PR body must list requirement IDs, exact tests run, observed results, evidence paths, migration/data impact,
  rollback/recovery notes, and external limitations.
- Update docs/FEATURE_COMPLETENESS.md or the generator that owns it after evidence changes.

## Session stop condition
A normal Codex session may stop only when:
- its selected coherent slice is fully implemented and evidence recorded, OR
- the next action requires a user-only credential/authorization or irreversible decision, OR
- every remaining dependency-eligible task is blocked externally/environmentally.
Do not stop merely because a plan was produced.
