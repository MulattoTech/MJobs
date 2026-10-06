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

`MJ-REQ-108.A06` establishes the provider contract and a deterministic no-AI implementation. It does not implement a remote model integration or complete parent requirement `MJ-REQ-108`.

## Interfaces and tests

- Public contract/factory: `services/ai/providers`.
- Contract tests: `tests/unit/test_disabled_ai_provider.py`.
- Higher-level matching behavior belongs in `services/matching`; provider-specific transport belongs in a provider implementation.
