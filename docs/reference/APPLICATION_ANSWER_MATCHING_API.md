# Maintainer reference: application answer matching API

**Source:** apps/api/application_answer_matching.py
**Current main source commit:** dec30c341f96fcb719458e6ecacb172904d93976
**Source blob:** 6264f8debd5ac44fd1aba12c1c71931febc2526f

## Purpose and route

create_application_answer_matching_router(store) builds a FastAPI router over the supplied local persistence store. The application mounts it in `apps/api/main.py:829–830`. Its route is:

    POST /profiles/{profile_id}/applications/{application_id}/answers/match

The request body must contain only question, or question plus expected_fingerprint. This is advisory retrieval: it returns eligible saved-answer candidates and approved story references, but does not select, approve, export, or submit an answer. Integration tests assert that matching does not alter the stored database/files and that conflicting answers are not returned (`apps/api/application_answer_matching.py:161–167`; `tests/integration/test_application_answer_matching_api.py:110–170, 217–231`).

## Inputs and validation

- question must be a nonblank string of at most 2,000 characters; surrounding whitespace is stripped before matching.
- expected_fingerprint is optional and, when present, must be exactly 64 lowercase hexadecimal characters.
- Unknown/missing keys, invalid body shape, empty question, or overlong question return 422 ANSWER_MATCH_INVALID_REQUEST. Invalid fingerprint returns 422 ANSWER_MATCH_INVALID_REVIEW_FINGERPRINT.
- Questions detected as sensitive by the local sensitive-term checks return 422 ANSWER_MATCH_SENSITIVE_QUESTION with review_gate.review_required=true and reason SENSITIVE_QUESTION. This check occurs before profile/application lookup (`apps/api/application_answer_matching.py:168–186`).

Safe synthetic test pattern:

    response = client.post(
        "/profiles/fixture/applications/fixture-app/answers/match",
        json={"question": "Which IT service management tools have you used?"},
    )

Use a local fixture app/store and synthetic profile answers only. This is an example request shape, not a command to contact any employer. The returned candidates may contain the saved answer text and story reference text; treat them as profile data and do not log real answers.

## Matching and eligibility flow

The handler loads the profile and application, concealing an application owned by another profile as 404. It resolves the application job through the profile-scoped store lookup, then fingerprints that job (`apps/api/application_answer_matching.py:188–207, 79–87`).

The endpoint reads the profile's saved-answer confidence policy and asks assess_answer_review to assess this question, including the optional expected fingerprint. It separately builds possible candidates, then intersects candidate IDs with the review assessment's matching IDs; if the review assessment requires review, it returns no candidates (`apps/api/application_answer_matching.py:208–223`).

A candidate is emitted only when its saved answer has a nonblank id, question, answer text and source; verified and user_approved are exactly true; confidence is a finite non-boolean number from 0 to 100 and meets the configured minimum; and the saved question is not sensitive. Question matching delegates to answer_service.match_answer_question using the policy threshold. Results are sorted by descending score then answer_id and capped at three. Candidate records include the original saved question/text/source, confidence, verification/approval flags, method, score and fixed explanation (`apps/api/application_answer_matching.py:30–76`).

Story references are independently filtered to current, APPROVED, reusable stories that explicitly allow APPLICATION_ANSWER and carry valid fact IDs/fingerprints. The endpoint returns reference text and fact provenance, not a new generated answer (`apps/api/application_answer_matching.py:89–150`).

Before returning, it rereads profile/application/job state; a missing row, changed profile version, changed application-job binding, or changed target-job fingerprint returns 409 ANSWER_MATCH_CONTEXT_STALE (`apps/api/application_answer_matching.py:224–241`). It then reruns the answer review with the original fingerprint; the stale-answer branch is specifically `current_answer_review["reason"] == "CHANGED_ANSWER"`, which returns the same 409 (`apps/api/application_answer_matching.py:242–252`). It also rereads story references before return and returns the same 409 if their `(story_id, revision)` binding changed (the handler’s story-revision check in `apps/api/application_answer_matching.py`).

## Response shape

A successful response has profile_id, application_id, the stripped question, candidates, review_gate, and story_references. Candidate method/explanation pairs are:
- EXACT_NORMALIZATION: exact question match after case/punctuation normalization.
- CURATED_SEMANTIC: curated ordinary paraphrase match.
- FUZZY: wording meets configured high-confidence threshold.

The code fixes the corresponding explanation strings (`apps/api/application_answer_matching.py:22–27, 63–76`). The review_gate contains review_required, reason, and fingerprint. The response also includes story references, filtered as described above. A synthetic test demonstrates exact and fuzzy candidates, advisory review metadata, and the absence of selection/approval/export fields (`tests/integration/test_application_answer_matching_api.py:110–170`).

## Error behavior and dependencies

- 422 ANSWER_MATCH_INVALID_REQUEST, ANSWER_MATCH_INVALID_REVIEW_FINGERPRINT, or ANSWER_MATCH_SENSITIVE_QUESTION for request validation/gating (`apps/api/application_answer_matching.py:168–186`).
- 404 ANSWER_MATCH_PROFILE_NOT_FOUND, ANSWER_MATCH_APPLICATION_NOT_FOUND, or ANSWER_MATCH_JOB_NOT_FOUND for missing or cross-profile context (`apps/api/application_answer_matching.py:188–207, 79–87`).
- 409 ANSWER_MATCH_CONTEXT_STALE for detected profile/application/job context drift or a `CHANGED_ANSWER` review (`apps/api/application_answer_matching.py:224–252`). The handler also returns this status when the story-revision binding changes before return (story-revision check in `apps/api/application_answer_matching.py`).
- 503 ANSWER_MATCH_STORY_REFERENCES_UNAVAILABLE when story-bank lookup raises StoryBankError (`apps/api/application_answer_matching.py:89–96`).
- Other store/service errors are not all converted by this router; the listed statuses are the explicit mappings in this source.

Dependencies are FastAPI request/router/error types, SQLAlchemy NoResultFound, the local answer review and answer-matching services, career-profile story bank, document answer service/policy, and target fingerprinting (`apps/api/application_answer_matching.py:7–13`). The route itself does not call a network client.

## Existing documentation and coverage

The API area guide does not list this endpoint (`apps/api/AREA.md:1–37`). The integration test is the detailed executable contract for API behavior (`tests/integration/test_application_answer_matching_api.py:19–108, 110–170, 217–289`). This reference describes the route contract; it does not establish broader application-workflow acceptance or runtime behavior.

## Evidence index

- API source: `apps/api/application_answer_matching.py:1–27, 30–76, 79–150, 161–252`.
- Router mount: `apps/api/main.py:829–830`.
- Integration tests: `tests/integration/test_application_answer_matching_api.py:19–108, 110–170, 217–289`.
- API area documentation check: `apps/api/AREA.md:1–37`.
- Runtime behavior and test acceptance are not established by this reference.
