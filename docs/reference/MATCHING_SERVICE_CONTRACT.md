# Maintainer reference: `services/discovery/matching.py`

- **Current main source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
- **Source path:** `services/discovery/matching.py`
- **Full source blob SHA:** `db2699e8e595e8ee4258d3590601850e903187d4`
- **Outcome:** `documented`
- **Verification boundary:** This reference describes source contracts and does not establish runtime behavior, product acceptance, or test/CI results.

## Documentation gap assessment

The exact pinned source exposes three public functions:

1. `build_preference_fit_factors(job, profile)` (`services/discovery/matching.py:454-533`)
2. `build_explanation(job, profile)` (`services/discovery/matching.py:544-629`)
3. `recalculate(store, profile_id, profile_version=None, check=None, *, candidate_job_ids=None, scoring_criteria=None)` (`services/discovery/matching.py:659-698`)

The first two already have function docstrings. The deterministic inventory’s single undocumented public definition corresponds to `recalculate`, which has no docstring. Existing architecture says explanations connect matching results to ingestion and that scores express suitability rather than invented qualifications or application authority (`docs/ARCHITECTURE.md:180-188`). The matching specification defines required/preferred/nice-to-have/unknown strength categories (`docs/spec/areas/matching_ai.md:162-173`). Neither describes the concrete builder outputs or transactional recalculation contract below.

## Module boundary and dependencies

The module performs deterministic local scoring against stored job/profile evidence and does not request provider data (`services/discovery/matching.py:1`). It depends on:

- SQLAlchemy selection;
- persistence models plus stable identity/upsert helpers;
- skill taxonomy normalization/extraction;
- certification and education parsing;
- title and salary normalization;
- positive approved skill-evidence collection;
- job/match audit-history writers;
- company-discovery request recording;
- optionally, selected-search scoring composition during recalculation (`services/discovery/matching.py:2-14`, `675-686`).

The pure builders do not receive a store. `recalculate` is the durable write boundary.

## `build_preference_fit_factors`

```python
build_preference_fit_factors(job, profile) -> list[dict]
```

Source: `services/discovery/matching.py:454-533`.

### Inputs

Both arguments are mapping-like objects read with `.get`:

- `job`: uses `attributes`, `title`, and `remote_type` directly; helper logic also interprets salary metadata and remote regions.
- `profile`: uses `preferences` and contact country/state through helper logic (`services/discovery/matching.py:454-533`; `80-167`).

Falsy/missing `attributes` or `preferences` become empty dictionaries. Truthy non-mapping values can fail with ordinary attribute/type errors because this function does not validate the outer schemas.

### Output

Returns five ordered, score-neutral factor dictionaries:

1. `salary` / “Salary expectation”
2. `title` / “Title preference”
3. `work_arrangement` / “Work arrangement”
4. `remote_location` / “Remote work region”
5. `employment_type` / “Employment type”

Every factor has:

```python
{
    "id": <stable code>,
    "label": <display label>,
    "status": "ALIGNS" | "NO_DIRECT_MATCH" | "UNKNOWN",
    "explanation": <fixed/local explanation>,
    "score_effect": "NONE",
}
```

The construction and fixed order are visible at `services/discovery/matching.py:454-533`.

### Comparison rules

- **Salary:** compares only a directly stated annual USD profile range with a complete finite annual USD employer/source-provided job range. Missing, foreign/mixed currency, invalid range, nonannual range, or ambiguous mixed `k` suffix yields `UNKNOWN` (`services/discovery/matching.py:170-206`, `464-479`).
- **Title:** compares local normalized title families for an exact normalized desired-role match; absence of either side yields `UNKNOWN` (`services/discovery/matching.py:224-226`, `481-499`).
- **Work arrangement:** normalizes `ONSITE` to `ON_SITE`, recognizes remote variants, and compares only known `REMOTE`, `HYBRID`, or `ON_SITE` values (`services/discovery/matching.py:501-517`).
- **Remote location:** recognizes a small explicit country alias set (US/Canada/UK), U.S. states, worldwide markers, and supported job remote regions. Unknown region/country evidence remains `UNKNOWN`; on-site commute and work authorization are never inferred (`services/discovery/matching.py:62-167`).
- **Employment type:** compares uppercased known job type with explicit configured preferences; missing/unknown evidence remains `UNKNOWN` (`services/discovery/matching.py:520-532`).

These factors are descriptive and `score_effect` is always `NONE`. Callers may explicitly compose them under another scoring profile, but this baseline builder does not penalize or reward the result (`services/discovery/matching.py:454-459`, `478-533`).

### Errors

There is no local exception handling. Malformed mapping structures and exceptions from title/salary normalization propagate. Unsupported or incomplete evidence generally produces `UNKNOWN` rather than an exception where the source explicitly checks it.

## `build_explanation`

```python
build_explanation(job, profile) -> dict
```

Source: `services/discovery/matching.py:544-629`.

### Purpose

Build a deterministic baseline score measuring **approved positive skill-evidence coverage**, plus transparent requirements and score-neutral fit factors. It does not decide whether a candidate is qualified and does not authorize an application (`services/discovery/matching.py:544-545`, `613-628`; `docs/ARCHITECTURE.md:180-188`).

### Inputs and skill normalization

- `job.attributes.required_skills`, `preferred_skills`, and `nice_to_have_skills` are canonicalized into sets (`services/discovery/matching.py:546-551`).
- `job.skills` is canonicalized, labeled, deduplicated case-insensitively, and preserves first occurrence order (`services/discovery/matching.py:555-563`).
- `profile.facts` is passed to `collect_positive_skill_evidence` for approved positive `skill` facts (`services/discovery/matching.py:552-554`).

Each job skill receives one strength:

- `REQUIRED`: weight `1.0`
- `PREFERRED`: weight `0.5`
- `NICE_TO_HAVE`: weight `0.25`
- `UNKNOWN`: weight `0.25`

The constants are defined at `services/discovery/matching.py:17`, and assignment precedence is required, then preferred, then nice-to-have (`services/discovery/matching.py:567-576`). When `skill_evidence == "structured_required_skills"`, otherwise-unclassified listed skills default to required (`services/discovery/matching.py:546-551`, `567-575`).

### Evidence treatment

A skill receives coverage only when its canonical/label evidence is in the approved positive evidence set (`services/discovery/matching.py:552-554`, `577-593`). Otherwise the row distinguishes:

- `CONFLICTING_APPROVED_EVIDENCE`
- `EXPLICIT_NEGATIVE_OR_UNCERTAIN_EVIDENCE`
- `NO_APPROVED_EVIDENCE`

and never turns missing evidence into a claim that the person lacks the skill (`services/discovery/matching.py:579-604`). Pinned unit tests cover negative, aspirational, conflicting, unapproved, invalid-confidence, custom-skill, and private-text boundaries (`tests/unit/test_positive_skill_evidence.py:22-95`). They were inspected, not run.

### Score

The baseline score is:

```text
round(100 * covered_weight / possible_weight, 2)
```

or `0` when no weighted skills exist (`services/discovery/matching.py:564-607`). This is coverage, not a probability or qualification verdict.

The method label is selected as:

- `structured_required_skills` when all listed skills are structurally required and there are no preferred/nice-to-have sets;
- `weighted_skill_evidence_coverage` when classified skill evidence exists or the evidence type is not absent/description-only;
- otherwise `description_skill_mentions` (`services/discovery/matching.py:608-612`).

Pinned integration assertions demonstrate the weighted calculation: required 1.0, preferred 0.5, and nice-to-have 0.25 produce a 42.86% score when 0.75 of 1.75 weight is covered (`tests/integration/test_explainable_matching.py:65-89`).

### Output

```python
{
    "score": <0..100 number>,
    "missing": [<sorted uncovered skill labels>],
    "components": {
        "method": <method code>,
        "skill_evidence_coverage": {
            "covered_weight": <number>,
            "possible_weight": <number>,
            "percentage": <same as score>,
        },
        "fit_factors": [<preference, certification, education, experience factors>],
        "matched": [<sorted covered skills>],
        "detected": [<deduplicated job skills>],
        "requirements": [
            {
                "skill": <label>,
                "strength": "REQUIRED" | "PREFERRED" | "NICE_TO_HAVE" | "UNKNOWN",
                "weight": <number>,
                "covered_weight": <number>,
                "profile_evidence": "APPROVED" | "NO_APPROVED_EVIDENCE",
                "evidence_reason": <stable code>,
                "explanation": <bounded explanation>,
            }
        ],
        "limitation": <fixed evidence/qualification warning>,
    },
}
```

Source: `services/discovery/matching.py:594-629`.

`fit_factors` contains the five preference factors plus locally parsed certification, education, and relevant-experience factors (`services/discovery/matching.py:536-541`). These are all informational with `score_effect: "NONE"` in this baseline explanation. Certification evidence requires approved, verified, sufficiently confident sourced facts; education handling explicitly preserves unknown/ambiguous/negative distinctions; relevant experience uses approved positive evidence rather than text overlap alone (`services/discovery/matching.py:259-451`).

### Errors and purity

The function has no exception wrapper. Malformed `job`, `attributes`, `skills`, `profile`, or fact structures can propagate ordinary type/key/value errors from local helpers. Recognized unsupported evidence usually yields an unknown/no-credit factor rather than fabricating certainty.

The function does not mutate persistence. A pinned unit test copies the profile and asserts it remains unchanged after explanation building (`tests/unit/test_positive_skill_evidence.py:45-51`).

## `recalculate`

```python
recalculate(
    store,
    profile_id,
    profile_version=None,
    check=None,
    *,
    candidate_job_ids=None,
    scoring_criteria=None,
) -> None
```

Source: `services/discovery/matching.py:659-698`. The function has no explicit return, so successful completion returns `None`.

### Inputs

#### `store`, `profile_id`, `profile_version`

`store` supplies the transactional engine, profile snapshot, audit helper, and persisted tables. `profile_id` selects the profile and match ownership. Optional `profile_version` pins the profile snapshot; `store.profile(profile_id, profile_version, connection=...)` validates it inside the transaction (`services/discovery/matching.py:663-670`). A stale/nonexistent pinned version propagates the store’s lookup error; a pinned test expects `NoResultFound` and no match write (`tests/unit/test_matching_candidate_scope.py:80-87`).

#### `check`

Optional zero-argument cancellation/authorization callback; defaults to a no-op. It is called once immediately before scoring each selected job (`services/discovery/matching.py:661-674`). Exceptions propagate and abort the surrounding transaction. If the candidate set is empty, this function does not call `check` because the call is inside the job loop.

Worker callers pass their operation lifecycle check for collection, rescore-only, and selected-search workflows (`workers/tasks.py:320-369`).

#### `candidate_job_ids`

Optional bounded scope:

- `None`: load all jobs ordered by job ID;
- otherwise must be a `list` or `tuple` of at most 10,000 entries;
- each entry must be a nonempty string no longer than 128 characters;
- values are deduplicated and sorted;
- database queries use chunks of 500, then results are sorted by job ID (`services/discovery/matching.py:632-656`).

Unknown IDs simply yield no row. An empty list performs no per-job scoring but still reaches downstream request recording and recalculation audit. Pinned tests assert deduplicated sorted scoring, preservation of unselected match history, legacy full-rescore behavior when omitted, version guarding, and invalid-scope rejection (`tests/unit/test_matching_candidate_scope.py:51-93`).

#### `scoring_criteria`

Optional selected-search criteria. When present, the function imports `compose_selected_search_match`, composes a selected-search score from the job, baseline score, criteria, and profile, replaces the persisted top-level score, and adds `scoring_profile` plus `scoring_contributions` to components (`services/discovery/matching.py:675-686`). The baseline requirements/fit explanation remains present.

### Transaction and locking

All work occurs in one `store.engine.begin()` transaction (`services/discovery/matching.py:663-698`).

- SQLite explicitly executes `BEGIN IMMEDIATE` (`services/discovery/matching.py:664-665`).
- Other dialects select the profile ID `FOR UPDATE` and require exactly one row (`services/discovery/matching.py:666-669`).

Any uncaught exception—including cancellation, missing profile/version, scoring failure, audit failure, or persistence failure—escapes the function and causes normal transaction rollback. The function contains no retry or partial-commit logic.

### Per-job effects

For each selected stored job, in deterministic ID order (`services/discovery/matching.py:648-656`, `672-696`):

1. call `check`;
2. build the baseline explanation;
3. optionally compose selected-search scoring;
4. detect whether this profile has any historical match for the job;
5. record `JOB_DISCOVERED` only when no profile match previously existed;
6. record `MATCH_SCORED` for the active profile version;
7. upsert/replace a match keyed by stable identity of job ID, profile ID, and profile version, storing the explanation.

After all jobs, it records company-discovery requests for the processed job IDs/profile and writes a `MATCHES_RECALCULATED` audit event with the actual profile version (`services/discovery/matching.py:687-698`).

The function does not delete matches outside `candidate_job_ids`; scoped recalculation intentionally preserves unrelated history. The pinned candidate-scope test verifies that behavior (`tests/unit/test_matching_candidate_scope.py:51-78`).

### Errors

Explicit candidate-scope validation raises `ValueError` with one of two messages:

- `Candidate job IDs must be a bounded list of IDs`
- `Candidate job IDs must contain non-empty IDs up to 128 characters`

Source: `services/discovery/matching.py:637-645`.

All other errors from profile lookup/locking, job reads, callbacks, explanation/scoring composition, audit writers, request recording, and upsert propagate unchanged. There is no error translation in this module.

## Safe examples

### Pure explanation

```python
result = build_explanation(
    {
        "title": "Systems Administrator",
        "skills": ["Windows Server", "Active Directory"],
        "attributes": {
            "required_skills": ["Windows Server"],
            "preferred_skills": ["Active Directory"],
            "skill_evidence": "structured_required_skills",
        },
    },
    {
        "facts": [],
        "preferences": {},
        "contact": {},
    },
)
print(result["score"], result["components"]["limitation"])
```

With no approved positive facts, the source contract yields no covered weight; the explanation must not be restated as proof that the person lacks either skill. This is an illustrative source-derived example and was not executed.

### Scoped durable recalculation

Use only a disposable/local store and known synthetic IDs because this call writes matches and audit/request records:

```python
recalculate(
    disposable_store,
    profile_id="fixture",
    profile_version=known_profile_version,
    check=lambda: None,
    candidate_job_ids=["synthetic-job-a", "synthetic-job-b"],
)
```

This does not contact providers, but it is not read-only. Do not use a production/user store for a documentation exercise.

## Maintainer cautions

- Baseline score is approved positive skill-evidence coverage, not qualification probability, hiring likelihood, or application authority (`services/discovery/matching.py:544-629`; `docs/ARCHITECTURE.md:180-188`).
- Preference, certification, education, remote-location, and relevant-experience fit factors are informational and score-neutral in this baseline (`services/discovery/matching.py:454-541`).
- Missing or conflicting profile evidence must not be rewritten as a negative biographical claim (`services/discovery/matching.py:579-604`, `627`).
- Scoped recalculation preserves unselected match history; do not add cleanup/deletion based only on `candidate_job_ids` (`services/discovery/matching.py:637-698`).
- Profile/search changes require fresh scoring. Do not present older versioned rows as current (`docs/ARCHITECTURE.md:180-188`).
- `recalculate` is transactional and durable even though scoring itself is local; cancellation or failure must propagate so the transaction can roll back (`services/discovery/matching.py:659-698`).
