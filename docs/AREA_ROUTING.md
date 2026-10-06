# Area Routing

Codex should **not** read the entire original specification for ordinary work. Load:
1. `AGENTS.md`
2. the active GitHub issue
3. the matching area spec below
4. the touched code/tests
5. the original source only if ambiguity remains

| Area | Requirement range | Intended code ownership | Area spec |
|---|---:|---|---|
| `governance_safety` | MJ-REQ-000…MJ-REQ-014 | `root / docs / planning` | `docs/spec/areas/governance_safety.md` |
| `core_search_model` | MJ-REQ-015…MJ-REQ-039 | `packages/domain; apps/api; services/search` | `docs/spec/areas/core_search_model.md` |
| `job_identity_dedupe` | MJ-REQ-040…MJ-REQ-053 | `services/normalization; services/dedupe` | `docs/spec/areas/job_identity_dedupe.md` |
| `company_ats_discovery` | MJ-REQ-054…MJ-REQ-084 | `services/company_discovery; services/ats_detection` | `docs/spec/areas/company_ats_discovery.md` |
| `job_lifecycle` | MJ-REQ-085…MJ-REQ-087 | `packages/domain/jobs; services/discovery` | `docs/spec/areas/job_lifecycle.md` |
| `career_profile_documents` | MJ-REQ-088…MJ-REQ-107 | `services/career_profile; services/documents` | `docs/spec/areas/career_profile_documents.md` |
| `matching_ai` | MJ-REQ-108…MJ-REQ-120 | `services/matching; services/ai` | `docs/spec/areas/matching_ai.md` |
| `application_automation` | MJ-REQ-121…MJ-REQ-154 | `services/applications; workers/applications` | `docs/spec/areas/application_automation.md` |
| `orchestration_data_security` | MJ-REQ-155…MJ-REQ-180 | `workers; packages/persistence; services/security; observability` | `docs/spec/areas/orchestration_data_security.md` |
| `web_api_analytics` | MJ-REQ-181…MJ-REQ-207 | `apps/web; apps/api; services/analytics` | `docs/spec/areas/web_api_analytics.md` |
| `test_harness` | MJ-REQ-208…MJ-REQ-230 | `tests; fixtures; mock-services` | `docs/spec/areas/test_harness.md` |
| `performance_learning_history` | MJ-REQ-231…MJ-REQ-247 | `services/discovery; services/learning; packages/persistence` | `docs/spec/areas/performance_learning_history.md` |
| `operations_ux_ci` | MJ-REQ-248…MJ-REQ-280 | `apps/web; infra; .github; cli` | `docs/spec/areas/operations_ux_ci.md` |
| `docs_verification` | MJ-REQ-281…MJ-REQ-289 | `docs; tools/verification` | `docs/spec/areas/docs_verification.md` |
| `delivery_control` | MJ-REQ-290…MJ-REQ-300 | `planning; docs; root` | `docs/spec/areas/delivery_control.md` |
| `agent_orchestration_safety` | MJ-REF-013, MJ-REF-014; MJ-REQ-000, 008, 121, 140 | `services/agents; packages/persistence` | `services/agents/AREA.md` |

## Ownership rule

When development begins, create target directories lazily. Each major code area should contain an
`AREA.md` stating its purpose, public interfaces, requirement IDs, test locations, and prohibited
cross-area dependencies. Do not create empty implementation skeletons merely to make the tree look complete.

Cross-cutting contracts belong in `packages/contracts` or `packages/domain`; provider-specific logic stays
inside the corresponding adapter/provider directory.
