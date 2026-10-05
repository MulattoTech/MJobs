# MJobs Progress Dashboard

Current reconciliation: 2026-10-05. Source matrices are authoritative; old percentages are historical.

GitHub automation: **DISABLED**. Local acceptance is separate from hosted checks and full release.
Original: **64 PASS / 76 PARTIAL / 226 NOT_RUN**, 366 total. Fully accepted: **17.49%**.
Reference: **1 PASS / 0 PARTIAL / 58 NOT_RUN**, 59 total.
Full release: **NOT_RUN**. Combined synthetic core journey: **local PASS for bounded paths**, not full requirements.

Legend: 🟩 PASS; 🟨 PARTIAL; ⬜ NOT_RUN. Bars are rounded navigation aids, not code or effort percentages.

| Area (including integration children) | Status distribution | PASS | PARTIAL | NOT_RUN | Total |
|---|---|---:|---:|---:|---:|
| application_automation | 🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 6 | 44 | 50 |
| career_profile_documents | 🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟨🟨🟨🟨🟨🟨🟨 | 13 | 7 | 0 | 20 |
| company_ats_discovery | 🟩🟩🟩🟩🟨🟨🟨🟨🟨🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜ | 10 | 20 | 23 | 53 |
| core_search_model | 🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 21 | 3 | 17 | 41 |
| delivery_control | ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 0 | 11 | 11 |
| docs_verification | ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 0 | 9 | 9 |
| governance_safety | 🟩🟩🟩🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨⬜⬜⬜⬜⬜ | 2 | 9 | 4 | 15 |
| job_identity_dedupe | 🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩 | 14 | 0 | 0 | 14 |
| job_lifecycle | 🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩 | 3 | 0 | 0 | 3 |
| matching_ai | 🟨🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 4 | 15 | 19 |
| operations_ux_ci | 🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 1 | 32 | 33 |
| orchestration_data_security | 🟨🟨🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 7 | 19 | 26 |
| performance_learning_history | 🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 1 | 16 | 17 |
| test_harness | 🟩🟨🟨🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 1 | 6 | 16 | 23 |
| web_api_analytics | 🟨🟨🟨🟨🟨🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ | 0 | 12 | 20 | 32 |

All 366 requirements, including adapter/integration children, are counted. Original parent-only tables did not account for every child.

## Remaining work
Broader resume rephrasing (#96), matching/search controls, answer-policy completeness, source breadth and runtime/release acceptance remain open. Product AI is not implemented.
Raw CLI transcripts remain private. See [current handoff](../planning/SESSION_HANDOFF.md), [local-only guide](GITHUB_WORKFLOW.md), and [reconciliation evidence](evidence/2026-10-05-local-only-reconciliation.json).
