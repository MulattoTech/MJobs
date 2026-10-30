# Maintainer reference: bounded AI candidate consumer

**Pinned source path:** `services/ai/consumer.py`
**Pinned source commit:** `2f01b876762fcbdd70f11e69865a48257503af93`
**Pinned source blob:** `07f01f54edb40481ed8d1a72914fbf5238a6cb59`

## Responsibility and boundary

`consume_ai_candidates` applies an `AICostPolicy` to a bounded iterable of `AIJobInput`, calls a structured-output router only for the policy's selected AI candidate IDs, validates the returned evaluation/evidence, and returns immutable tuples of evaluations and separately review-gated document candidate IDs (`AIConsumption`, source lines 90–95 and 152–228). The module explicitly says it never creates or publishes documents (line 163). Deterministic match scores remain input to the policy and prompt; the AI classification is advisory and does not alter the deterministic score (lines 48–53, 103–118; `services/ai/AREA.md`, lines 40–46).

At the product boundary, AI evaluation is initiated through the profile-scoped `POST /profiles/{profile_id}/ai-cost-plan/evaluate` flow described in `services/ai/AREA.md` line 40. The consumer is its service implementation, not the HTTP route itself. The area guide also explains explicit user initiation and separate document review/preparation (lines 40–46); this note adds the detailed function contract absent from that overview.

## Inputs and delegated policy

`AIJobInput` is a frozen value with `job_id`, deterministic score, title, company, description, and `reviewed=False` default (lines 63–87). Construction rejects blank/untrimmed/over-200-character IDs, scores other than exact integers 0–100, non-string text, title/company over 512 characters, and a non-boolean review flag, with stable `ValueError` codes shown in those lines. Description length is not rejected here; it is bounded before provider prompt construction.

The function requires an `AICostPolicy` and either an `AITaskRouter` or a compatible object exposing callable `generate_structured`; the default per-call timeout is 30 seconds and whole-batch timeout 120 seconds. Optional `check` is called before and after each provider call; optional `cancellation_event` is forwarded; `clock` can supply a monotonic-time function for deterministic tests (lines 152–180). Both timeout values must be numeric (not bool) and in `(0, 600]`; invalid policy/router/timeouts/clock/input raise `ValueError` with the `AI_FUNNEL_*` / `AI_TASK_*` codes at lines 165–180.

The consumer reads at most `policy.max_input_candidates + 1` items, then passes `CandidateScore(job_id, deterministic_score, reviewed)` values to `policy.plan` (lines 181–184). The policy is responsible for validating its cap, stable identity deduplication, minimum score threshold, score ordering, AI cap, and reviewed-document cap; see `services/ai/cost_controls.py` lines 50–68 and 132–170. Conflicting duplicate candidate identities are rejected (`AI_FUNNEL_DUPLICATE_CONFLICT`, consumer lines 186–191). The function materializes this bounded input before provider calls.

## Prompt and structured result contract

The generated prompt contains score, title, company, and up to 8,000 description characters, with an omission marker for a truncated tail; the final prompt is capped at 12,000 characters (lines 19–20, 98–118). It labels posting text untrusted, instructs the model not to follow embedded instructions or invent applicant/profile facts, and asks for a concise rationale and at most three exact posting excerpts. Excerpts must identify `title` or `description` and copy 8–280 characters (lines 41–61, 103–118).

`validate_job_evidence` checks each quote as an exact substring of the title (first 512 chars) or description (first 8,000 chars), rejects blank, duplicate, unsupported, or truncated-tail quotes, and raises `StructuredOutputError("AI_STRUCTURED_OUTPUT_INVALID")` (lines 121–132). The evaluation model restricts category to `STRONG`, `REVIEW`, or `WEAK`; rationale is 1–1,000 characters and must be nonblank; evidence has at most three entries (lines 41–60). Quotes support inspection of job text, not proof of applicant qualifications (model docstrings and prompt lines 42, 110–112).

For each planned AI ID, `generate_structured` receives `task=AITask.JOB_SCORING`, the prompt, `result_model=AIJobEvaluation`, and a timeout clipped to the remaining whole-batch deadline (lines 193–211). A response after the deadline is discarded by raising `AIConsumerTimeoutError` (`AI_TASK_TIMEOUT`, lines 216–217). The returned object must be an `AIJobEvaluation` and pass quote validation (lines 220–223).

## Failure and output behavior

- An `AIProviderError` with code `AI_PROVIDER_CANCELLED` becomes `AIConsumerCancelledError` (`AI_TASK_CANCELLED`); other provider errors propagate unchanged (lines 212–215). A supplied cancellation event is passed to the router but the consumer itself does not forcibly interrupt a synchronous provider call.
- Total budget exhausted before a call or before accepting its result raises `AIConsumerTimeoutError` (`AI_TASK_TIMEOUT`, lines 199–201, 216–217). Provider-specific errors other than cancellation are not converted here.
- A bad structured result type raises `ValueError("AI_STRUCTURED_OUTPUT_INVALID")`; malformed/unsupported quotes raise `StructuredOutputError` with the same stable code (lines 220–223, 121–132).
- Successful output is `AIConsumption(evaluations=((job_id, evaluation), ...), document_candidate_ids=(...))` (lines 90–95, 225–228). Document candidate IDs are a separate policy output; this consumer does not prepare or persist documents.
- `public_job_evidence` only reports `VERIFIED_QUOTES` for a completed checkpoint whose validated category/rationale match the supplied item and whose quotes revalidate against the raw candidate; malformed/legacy/no-quote cases return `UNKNOWN` with no quotes (lines 134–149).

## Offline, synthetic usage example

The following example uses a local fake router; it makes no provider request and creates no documents:

```python
from services.ai.consumer import AIJobEvaluation, AIJobInput, consume_ai_candidates
from services.ai.cost_controls import AICostPolicy

class SyntheticRouter:
    def generate_structured(self, **request):
        assert request["result_model"] is AIJobEvaluation
        return AIJobEvaluation(category="REVIEW", rationale="Synthetic advisory result.")

result = consume_ai_candidates(
    [AIJobInput("fixture-job", 90, "Analyst", "Example Co", "Synthetic posting")],
    AICostPolicy(max_ai_candidates=1, max_document_candidates=1),
    SyntheticRouter(),
)
assert result.evaluations[0][0] == "fixture-job"
```

For production, pass the configured `AITaskRouter` from the authorized API/workflow. Do not interpret a category or quotation as verified applicant qualifications, treat generated text as trusted, or assume that `document_candidate_ids` imply user review or completed document generation.

## Existing docs/tests and evidence limits

`services/ai/AREA.md` lines 36–48 already documents provider configuration, cost-policy overview, user-initiated evaluation, advisory results, separate document-review gates, structured-output safety, and the fact that live configured inference is unverified. It does not spell out this consumer's exact input/call/time-bound/evidence contracts, so this reference complements rather than duplicates it.

Relevant existing tests inspected at the pinned commit:
- `tests/unit/test_ai_cost_consumer.py` lines 18–72: disconnect cancellation checks, total-deadline clipping and late-result rejection.
- Same file lines 94–170: policy-selected provider calls, no document generation, input bound and untrusted-description truncation.
- `tests/unit/test_ai_source_quotes.py` lines 8–43: quote schema and rejection of omitted-tail, mismatched, duplicate, blank, and unsupported evidence.

This is source-pinned documentation only. The requested blob was verified with Git. No tests, CI, live provider inference, or API/browser flow were run for this task; test references describe existing assertions, not fresh results.
