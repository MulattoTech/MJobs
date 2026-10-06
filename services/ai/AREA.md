# AI Services Area

Owns provider-neutral AI contracts and explicitly configured model integrations for MJobs.

## Boundaries

- Provider integrations live under `services/ai/providers` and implement the shared provider contract.
- Provider capabilities must be declared exactly; callers must not infer unsupported behavior.
- The disabled provider performs no network access and fails closed for generation requests.
- AI output remains untrusted input and must be validated by the consuming service.
- Never put prompts, user data, credentials, or provider responses in exception messages or logs by default.
- Never bypass access controls, anti-bot protections, CAPTCHA, MFA, or rate limits.

## Current slice

`MJ-REQ-108.A06` establishes the provider contract and a deterministic no-AI implementation. `services/ai/tasks.py` defines seven independent task-to-provider/model routes with disabled defaults. Neither slice implements a remote model integration or completes requirements `MJ-REQ-108` or `MJ-REQ-109` by itself.

## Interfaces and tests

- Public provider contract/factory: `services/ai/providers`.
- Task configuration/router: `services/ai/tasks.py`.
- Profile-scoped persisted task selections: `packages/persistence/store.py`, exposed by `/profiles/{profile_id}/ai-task-settings`.
- Contract tests: `tests/unit/test_disabled_ai_provider.py`, `tests/unit/test_ai_task_configuration.py`.
- Higher-level matching behavior belongs in `services/matching`; provider-specific transport belongs in a provider implementation.

`AITaskConfiguration.from_mapping` accepts a task-keyed mapping of non-secret `provider` and `model` identifiers. Omitted task entries default to `disabled`; `AITaskRouter` passes the selected model only to the selected provider. The default factory rejects providers that do not yet exist, so configuration cannot imply that an unavailable remote model works.

Users can read and save a complete seven-task selection for the active profile in AI Settings. Missing selections resolve to disabled; currently registered choices are disabled and loopback-only Ollama. The durable table stores only provider and model identifiers, never endpoint credentials or prompts. The API validates all settings before the atomic save, and the UI reloads settings when the active profile changes.

`AICostPolicy` applies a configurable minimum deterministic score and per-batch ceilings after stable identity deduplication. It bounds input iteration, rejects conflicting duplicate identities, selects at most the configured AI candidates, and permits document candidates only when they carry an explicit review flag. The planner performs no provider calls or document generation. On the Codex MJ-REQ-110 lane, `services/ai/consumer.py` consumes this plan for profile-scoped job scoring through `POST /profiles/{profile_id}/ai-cost-plan/evaluate`. The route preserves deterministic scores and creates no documents. This is a bounded consumer slice, not proof of configured live inference or completion of the broader AI document workflow.

Users can persist per-profile thresholds through `/profiles/{profile_id}/ai-cost-policy`. Product settings may lower, but cannot raise, the default maximum of 75 AI candidates and 20 reviewed-document candidates. `/profiles/{profile_id}/ai-cost-plan` previews stored deterministic match scores and returns counts only; it makes no provider calls and creates no documents. The evaluation route uses the saved profile task configuration and cost policy; disabled providers fail closed, and structured provider errors are mapped to sanitized upstream errors. `MJ-REQ-110` remains partial while live provider inference and a reviewed AI-document workflow are unavailable.

`StrictAIResult` models use strict Pydantic validation and forbid additional object properties, including nested schema definitions. `AITaskRouter.generate_structured` supplies the declared JSON Schema only to a provider that explicitly advertises structured-output support, then parses a bounded JSON object and rejects duplicate keys, non-standard constants, schema violations, and permissive nested schemas with stable errors that omit prompt and response content. The disabled provider stays closed; output remains untrusted evidence for downstream truthfulness checks. The bounded job-scoring consumer is implemented on the active Codex lane; real provider inference and consumers for other AI tasks are not verified.

`OllamaProvider` adds an opt-in local HTTP model path constrained to numeric loopback addresses, with proxy variables ignored and bounded request/response handling. No installed Ollama service is assumed, and tests do not perform inference.
