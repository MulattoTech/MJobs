# MJ-REF-030 — Provider/task offline completeness matrix (follow-on slice)

Run: web-20261104T071144Z-6c492d7b1395
Claim: refs/heads/webpro-claims/MJ-REF-030 at df43667c4dbe4766b7177ed6a6b88b86eeb65dce
Baseline main: 95d02bd0831be4ba838bbfdd9356db108a77390b
Issue: https://github.com/MulattoTech/MJobs/issues/400
Status: **PARTIAL pending exact-head CI, protected merge and real producer integration**.

## Source of truth and scope

- Six enabled factory-selectable provider names, including the azure alias, are explicitly declared in services/ai/providers/__init__.py. Unknown names remain rejected; disabled is not an LLM provider.
- Existing seven AI_TASKS are the task denominator. The checked-in manifest enumerates each of 42 provider/task pairings once; missing, duplicated or invented pairs fail CI.
- Four prompts are rendered from the actual PromptRegistry. Job scoring uses the actual consumer _prompt with synthetic AIJobInput.
- Requirement extraction and company summarization use explicitly **caller-supplied test fixture prompts**, not invented shipped default prompts. The receipt exposes these two exceptions.
- Four tasks use strict JSON models (real AIJobEvaluation for scoring and a new canonical JobParsingResult); three use explicit text output.
- Each real provider class is checked for declared text-generation and structured-output capabilities; losing either fails closed. The separate fake tests routing and response contracts, not actual provider transport.
- Each pairing routes a typed provider/model choice through the real AITaskRouter to an in-process recording fake, asserting task/model/prompt/timeout/schema. No live model is called.

## Safety, failure and privacy

- No external AI/network requests, user profile content, credentials, applications or production settings.
- No dynamic path imports or executable commands from fixture content.
- Unknown providers/tasks/schemas, malformed/missing/duplicate combinations, unsafe prompt provenance, omitted variables, invalid outputs, secret-shaped fixture text and contract mismatches fail with stable classifications.
- Success/error receipts are bounded machine-readable JSON in the existing 7-day Python CI artifact. Existing mandatory MJ-REQ-278 unit/frontend commands remain intact.

## Reproducible tests

- Targeted pytest on final source: 28 tests and 36 subtests — PASS, including two negative provider capability drift cases.
- Offline provider-task matrix: six providers, seven tasks, 42 pairings, 24 structured, 18 text, zero model calls — PASS.
- Repository policy: zero errors — PASS.
- Acceptance catalogue validation — PASS.
- Git whitespace diff check — PASS.
- Ruff on the new matrix checker and test modules — PASS (zero issues); legacy provider/schema modules retain unrelated preexisting style findings outside the enforced CI quality scope.
- Mypy with the repository configuration — PASS (three configured source files).

## Explicit limitations

The synthetic-stub matrix does not prove live provider transport, real remote credentials, billing, cost controls, adversarial employer inputs, or complete product flows. Two tasks currently have only caller-supplied prompt fixtures, not an integrated source-owned producer. This delivery must not be used alone to call issue #400 or full MJobs acceptance PASS.

Protected CI, merge revision and production state are PENDING at document authoring. Rollback: revert this scope's source/tests/CI commit; there is no database migration or user-data rewrite.
