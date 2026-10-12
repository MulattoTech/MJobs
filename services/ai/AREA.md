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

`AICostPolicy` applies a configurable minimum deterministic score and per-batch ceilings after stable identity deduplication. It bounds input iteration, rejects conflicting duplicate identities, selects at most the configured AI candidates, and permits document candidates only when they carry an explicit review flag. The planner performs no provider calls or document generation. On the Codex MJ-REQ-110 lane, `services/ai/consumer.py` consumes this plan for profile-scoped job scoring through `POST /profiles/{profile_id}/ai-cost-plan/evaluate`. The AI Settings screen requires an explicit user action after preview before it calls that route, then displays the returned advisory evaluations for review. The route preserves deterministic scores and creates no documents. The lane also supports bounded review and preparation through the existing factual document workflow; live configured inference remains unverified.

Users can persist per-profile thresholds through `/profiles/{profile_id}/ai-cost-policy`. Product settings may lower, but cannot raise, the default maximum of 75 AI candidates and 20 reviewed-document candidates. `/profiles/{profile_id}/ai-cost-plan` previews stored deterministic match scores and returns counts only; it makes no provider calls and creates no documents. The evaluation route uses the saved profile task configuration and cost policy; disabled providers fail closed, and structured provider errors are mapped to sanitized upstream errors. The local provider route and durable review-gated factual document-preparation path are connected; live configured-provider inference remains NOT_RUN, so `MJ-REQ-110` remains partial.

Explicit evaluation results are stored as profile-versioned runs in the durable store with the policy and provider/model identifiers used, and can be reloaded through `GET /profiles/{profile_id}/ai-cost-plan/evaluations`. Results are saved only when the profile version still matches the snapshot used to build the request. The browser shows recent history per profile. These records remain advisory and do not modify deterministic matches.

Document preparation from an evaluation is a separate two-step human-reviewed workflow. The user must affirm that they reviewed a specific advisory result; `POST /profiles/{profile_id}/ai-cost-plan/evaluations/{run_id}/review-documents` persists that choice only for the evaluation's profile and unchanged profile version, enforcing that run's snapshotted document-candidate cap. `POST /applications` accepts the run ID for this path only after the matching durable review exists. The UI then requires the existing separate Prepare documents action; this uses the current factual document workflow, creates no AI-generated text, and does not submit externally. Review records are durable and profile-scoped.

`StrictAIResult` models use strict Pydantic validation and forbid additional object properties, including nested schema definitions. `AITaskRouter.generate_structured` supplies the declared JSON Schema only to a provider that explicitly advertises structured-output support, then parses a bounded JSON object and rejects duplicate keys, non-standard constants, schema violations, and permissive nested schemas with stable errors that omit prompt and response content. The disabled provider stays closed; output remains untrusted evidence for downstream truthfulness checks. The bounded job-scoring consumer is implemented on the active Codex lane; real provider inference and consumers for other AI tasks are not verified.

`OllamaProvider` adds an opt-in local HTTP model path constrained to numeric loopback addresses, with proxy variables ignored and bounded request/response handling. Per-request timeouts retain a distinct sanitized `AI_PROVIDER_TIMEOUT` classification. The structured consumer also applies one 120-second monotonic budget to the complete sequential evaluation batch and passes each call only its remaining time; late results are discarded. No installed Ollama service is assumed, and tests do not perform inference.

Advisory evaluation now uses durable admission: both the original evaluate action and `/evaluate/queued` return HTTP 202 with an operation ID. The existing outbox dispatches `AI_EVALUATE_QUEUED` to the dedicated `mjobs-ai` worker. Private batch rows retain bounded immutable job inputs, the profile version, selected configuration and policy; generic operation metadata contains only identifiers and a snapshot digest. Owner-scoped operation list/read/cancel interfaces support reload continuity. Validated candidate responses are checkpointed, and the complete advisory history and completed operation publish in one fenced transaction. Cancellation or a changed profile prevents partial publication. If a worker dies after starting a provider call without saving its response, recovery pauses with `AI_PROVIDER_OUTCOME_UNKNOWN`; it never automatically repeats that potentially billable call.

The AI Settings browser follows these saved operations instead of holding an inference request open. Compose runs a separate restricted-database AI worker and includes it in backup quiescence and restart recovery. Optional inference endpoints must be reachable from that worker under the existing provider transport restrictions; loopback refers to the worker's own runtime, not the host. No inference service is installed or started automatically. Saved encrypted credential routing and real configured-provider inference remain separate, unverified work; the durable evaluation slice does not promote either parent requirement or live provider support.
