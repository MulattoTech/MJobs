# Match explanation API maintainer reference

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/apps/api/matching_explanations.py).


**Source path:** `apps/api/matching_explanations.py`
**Source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
**Source blob:** `4dbbdfdf40d8ac7afc7a32a01c3f8443f7180a12`

## Registration and dependencies

`create_matching_explanations_router(store)` registers a read-only GET endpoint and closes over the persistence store [source: `apps/api/matching_explanations.py:162-259`]. The application mounts it from `apps/api/main.py` [registration: `apps/api/main.py:818-822`]. Dependencies are FastAPI router/errors, SQLAlchemy select/`NoResultFound`, persistence table models, and `resolve_role_scoring_profile` [source: `apps/api/matching_explanations.py:3-11`].

The route is:

```text
GET /profiles/{profile_id}/search-profiles/{search_profile_id}/jobs/{job_id}/match-explanation
```

It reads the profile, a search profile owned by that profile, the requested job, and a match row for that profile’s **current** version [source: `apps/api/matching_explanations.py:166-204`]. A synthetic integration fixture builds the router on a temporary SQLite store and calls the route with `TestClient` [test: `tests/integration/test_matching_explanations_api.py:17-34,85-86`].

## Success payload

The response contains `profile_id`, `search_profile_id`, `job_id`, saved `score`, `resolution` (`CURRENT` or `LEGACY_DEFAULT`), `profile_resolution`, `scoring_profile`, and `scoring_contributions` [source: `apps/api/matching_explanations.py:248-257`]. A contribution has a unique nonempty `dimension`, `weight`, nullable `raw_score` and `weighted_contribution`, nonempty `explanation`, and provenance with required `source` plus optional `evidence_ref`. Numeric values must be finite and in `[0,100]`; null raw/weighted values must occur together. Invalid or duplicate dimensions, malformed fields, or unsupported provenance keys produce stale-context 409 [source: `apps/api/matching_explanations.py:22-25,45-89`].

For a current configured profile, stored scoring-profile attribution must match the search criteria’s current resolved profile ID, version, role family and selection; the response returns only profile ID/version/role family plus the resolution explanation [source: `apps/api/matching_explanations.py:208-235`]. Missing configured dimensions with nonzero current weight are added as explicit `UNKNOWN` contributions (`raw_score` and `weighted_contribution` null); zero-weight and already represented dimensions are not synthesized [source: `apps/api/matching_explanations.py:91-111`].

For legacy matches without a selected profile, the route reports `LEGACY_DEFAULT`. If no stored contributions exist, it synthesizes a skills baseline preserving the saved score [source: `apps/api/matching_explanations.py:215-247`]. The integration tests assert both this compatibility path and a current profile with an explicit unknown dimension; they also confirm the GET leaves the database snapshot unchanged [test: `tests/integration/test_matching_explanations_api.py:110-180`].

## Score integrity check

For configured profiles, stored contribution weights must equal the current profile weights (with `skills` defaulting to 100 if absent); otherwise the API returns 409 `MATCH_EXPLANATION_STALE_CONTEXT` [source: `apps/api/matching_explanations.py:114-135`]. Rows with unknown raw scores do not contribute to the denominator. For known rows, scores below the dimension threshold are treated as zero. The handler verifies each stored weighted contribution equals `round(weight * effective_score / active_weight, 2)`, then verifies the recomposed score equals the saved match score [source: `apps/api/matching_explanations.py:124-159`]. When total active weight is zero (including an all-unknown configured profile), validation skips recomposition and preserves the saved score [source: `apps/api/matching_explanations.py:142-144`].

A safe fixture request example uses only the synthetic IDs seeded by the integration test:

```python
response = client.get(
    "/profiles/fixture/search-profiles/selected-search/jobs/job/match-explanation"
)
# In the seeded legacy fixture, expect HTTP 200 and resolution LEGACY_DEFAULT.
```

Do not substitute real profile/job identifiers in diagnostics. The test asserts the serialized score/explanation shape and confirms no writes [test: `tests/integration/test_matching_explanations_api.py:110-129`]. Separate contribution tests verify recomposition and that inconsistent stored data fails closed without writes [test: `tests/integration/test_matching_explanation_contributions.py:110-228`].

## Errors and stale-context distinction

The route returns 404 with stable codes for a missing profile, profile-owned search, job, or match: `MATCH_EXPLANATION_PROFILE_NOT_FOUND`, `MATCH_EXPLANATION_SEARCH_NOT_FOUND`, `MATCH_EXPLANATION_JOB_NOT_FOUND`, or `MATCH_EXPLANATION_MATCH_NOT_FOUND`. If no current-version match exists but an older match for that profile/job does, the result is 409 `MATCH_EXPLANATION_STALE_CONTEXT`, not “no match” [source: `apps/api/matching_explanations.py:169-204`; tests: `tests/integration/test_matching_explanations_api.py:184-215`].

Malformed current scoring-profile context, invalid score/contributions, profile-version mismatch, stale saved profile attribution, or recomposition mismatch also fail closed as 409 `MATCH_EXPLANATION_STALE_CONTEXT` [source: `apps/api/matching_explanations.py:28-42,114-159,203-247`]. Profile lookup’s `NoResultFound` is translated to the explicit 404; SQL and other unhandled failures are not broadly suppressed by this route [source: `apps/api/matching_explanations.py:169-175`].

## Existing docs and evidence boundary

The product review guide covers the user task of inspecting opaque scores and unknown evidence, not the API contract [docs: `docs/GUIDED_PRODUCT_REVIEW.md:75-87`]. Source and test references above were inspected at the pinned commit; no tests, API server, or CI were run for this reference. It does not claim acceptance or current production behavior.
