# MJobs Repository Map

This is the intended ownership map, not a command to generate every directory immediately.

```text
/
├─ AGENTS.md
├─ CODEX_START.md
├─ apps/
│  ├─ api/
│  └─ web/
├─ packages/
│  ├─ domain/
│  ├─ contracts/
│  ├─ persistence/
│  └─ shared/
├─ services/
│  ├─ discovery/
│  │  └─ sources/
│  ├─ company_discovery/
│  ├─ ats_detection/
│  │  └─ providers/
│  ├─ normalization/
│  ├─ dedupe/
│  ├─ matching/
│  ├─ ai/
│  │  └─ providers/
│  ├─ career_profile/
│  ├─ documents/
│  ├─ applications/
│  │  └─ adapters/
│  ├─ notifications/
│  ├─ analytics/
│  └─ security/
├─ workers/
│  ├─ discovery/
│  ├─ company_discovery/
│  ├─ documents/
│  ├─ ai/
│  └─ applications/
├─ migrations/
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ browser/
│  ├─ contract/
│  ├─ e2e/
│  └─ security/
├─ fixtures/
├─ mock-services/
│  ├─ aggregator/
│  ├─ company_site/
│  └─ ats/
├─ infra/
│  └─ docker/
├─ cli/
├─ tools/
│  └─ verification/
├─ docs/
│  ├─ spec/
│  ├─ decisions/
│  ├─ runbooks/
│  └─ evidence/
└─ planning/
```

## Rule of locality
Implementation belongs in the narrowest responsible area. Provider quirks must not leak into generic
domain code. UI code must not become the source of truth for durable workflow state.

## AREA.md contract
When an area receives its first implementation PR, create `<area>/AREA.md` with:
- responsibilities and non-responsibilities
- public interfaces
- owned requirement IDs
- dependencies
- test commands
- persistence touched
- security/truthfulness constraints
- extension instructions
