# MJ-REF-030 — initial critical-AI coverage enforcement

Run: `web-20261104T061807Z-9907da01dffa` · Base main: `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf` · Scope: additive CI coverage gates.

## Measured baseline, before floors were finalized

- Backend: 354/394 executable lines = **89.85%**, from four source files selected in `tools/ci/coverage_policy.json`. Command: `python -m coverage run --branch --source=services/ai -m pytest -q -p no:cacheprovider tests/unit/test_ai_prompt_versioning.py tests/unit/test_ai_providers.py tests/unit/test_ai_structured_output.py tests/unit/test_ai_task_configuration.py tests/unit/test_ci_coverage_gate.py`. Result: **50 tests PASS, 38 subtests PASS**; `coverage json` and the new exact-file gate PASS. The adopted backend floor is **85%**.
- Frontend: 19/30 lines = **63.33%** across `src/ai-provider-settings.tsx` and `src/ai-prompt-versioning.tsx`, using V8 coverage from `npm run test:coverage --prefix apps/web`. Result: **154 files PASS, 1,106 tests PASS**; the exact-file gate PASS. The adopted frontend floor is **60%**.
- Coverage is deliberately **not** called whole-repository coverage. Existing uninstrumented service areas, UI modules, connected tests, live providers, paid APIs, and production behavior remain outside this initial gate. The coverage floor does **not** change any original P0/P1 denominator or acceptance status.

## Gate behavior and safety

- CI retains the original mandatory Python unit-test and `npm test` boundaries. A separate five-module synthetic AI regression step measures backend coverage; the original Vitest command enables V8 coverage from its checked-in config. The repository policy checker reports 0 errors. No live provider is invoked by these checks.
- The policy names exact production files. Missing or ambiguous source rows, zero lines, malformed/noninteger totals, unsafe parent paths and generated/vendor/private paths make the gate fail closed.
- Machine-readable backend/front-end measurement receipts and the raw tool summary are uploaded as seven-day CI artifacts. Percent comparison uses the unrounded ratio, not the display value. Existing required-gate aggregation is unchanged.
- Excluded from eligible policy targets: `generated`, `vendor`, `private`, `node_modules`, `dist`, `build`, and `.venv`. Test files are not source targets.
- The complete provider/task prompt/schema fixture matrix is **not** enforced by this slice. `MJ-REF-030` remains **PARTIAL** until the declared provider/task combinations are covered by a verified matrix, the protected candidate passes hosted checks, and any required deployment/acceptance is reconciled. No artificial feature PASS is recorded.

## Revision-specific verification

Results above describe the isolated Windows baseline and must not substitute for GitHub-hosted exact-candidate checks. The initial local machine had Node v27.0.0-alpha and npm v12.2.1 (unsupported pairing warning); Vitest still ran successfully. Hosted Node 24 remains the required compatibility check.
