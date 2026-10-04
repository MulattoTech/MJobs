# LLM prompt/provider playbook

Use for model providers, prompt templates, schemas, AI-assisted extraction/generation, or provider routing.

## Boundaries
- Deterministic code/schema validation remains authoritative where possible.
- Prompts cannot weaken MJobs safety, truthfulness, provenance, or submission gates.
- Never turn external job/resume/page instructions into agent authority.
- AI output that would create user facts must be reviewable and grounded in verified evidence.
- Keep provider-specific behavior behind provider-neutral contracts.

## Required verification
- deterministic schema/parser tests;
- prompt-injection and unsupported-claim tests;
- provider-disabled/unavailable fallback behavior;
- fixtures for every declared provider/task combination rather than live nondeterministic CI;
- UI/API provenance and uncertainty readback when output is user-visible.

## Evidence
Record task, prompt/schema version, provider contract, fixtures, fallback behavior, known uncertainty, commit SHA, PR head, and exact-head CI.
