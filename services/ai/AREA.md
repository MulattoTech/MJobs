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
- Contract tests: `tests/unit/test_disabled_ai_provider.py`, `tests/unit/test_ai_task_configuration.py`.
- Higher-level matching behavior belongs in `services/matching`; provider-specific transport belongs in a provider implementation.

`AITaskConfiguration.from_mapping` accepts a task-keyed mapping of non-secret `provider` and `model` identifiers. Omitted task entries default to `disabled`; `AITaskRouter` passes the selected model only to the selected provider. The default factory rejects providers that do not yet exist, so configuration cannot imply that an unavailable remote model works.
