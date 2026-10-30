# Maintainer reference: `services/documents/cover_letter_preview.py`

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/services/documents/cover_letter_preview.py).


## Pinned source and verification boundary

- **Source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
- **Source path:** `services/documents/cover_letter_preview.py`
- **Full Git blob SHA:** `12ff8f83a9aefba795fcb256d726f7272337bb0a`
- **Method:** static inspection of that immutable blob, its deterministic cover-letter/fingerprint dependencies, focused tests, API adapter, specification, and historical evidence.
- **Verification status:** examples, tests, CI, API/database/browser flows, network behavior, document persistence, and external submission were **NOT_RUN**. Historical evidence is context only, not current acceptance.

This module builds a deterministic, read-only cover-letter preview and a provenance envelope from current saved profile, job, and career-fact snapshots. It does not save an application or document and returns explicit `read_only: True` / `document_created: False` markers (`services/documents/cover_letter_preview.py:1,116-132,259-284`).

## Dependencies and side-effect boundary

Direct dependencies are standard-library hashing/JSON/regex plus:

- `services.documents.tailor` for target and source-fact fingerprints;
- `services.documents.cover_letter._approved_company_context` for reviewed company-context validation;
- `services.documents.cover_letter.build_cover_letter` for deterministic paragraph/fact selection;
- `PreparationError` for stable document failure codes (`services/documents/cover_letter_preview.py:2-10`).

The service itself performs no database, filesystem, network, AI-provider, audit, application, or document write. The API adapter loads saved rows before calling it, then re-reads profile/job state after rendering to detect profile-version/job-fingerprint changes during rendering (`apps/api/cover_letter_preview.py:35-95`). Direct service callers do **not** receive that post-render database re-read; they must provide one coherent current snapshot themselves.

## `cover_letter_preview_fingerprint(job: dict) -> str`

**Source:** `services/documents/cover_letter_preview.py:22-60`

The function validates that `job` is a dictionary (including subclasses) with a nonblank exact-string `id`, then hashes a canonical JSON snapshot. The result is a lowercase 64-character SHA-256 hex string.

The snapshot includes:

- the lower-level target fingerprint;
- `id`, `company_id`, `company`, `title`, `skills`, `description`, and `responsibility_evidence`;
- selected job attributes: `skill_evidence`, `required_skills`, `preferred_skills`, `company_identity`, and `approved_company_context` (`services/documents/cover_letter_preview.py:22-55`).

The lower-level target fingerprint separately binds job ID/title/skills, description SHA-256, and parsed target responsibilities (`services/documents/tailor.py:176-185,201-210`). The preview fingerprint therefore detects company/context-only changes in addition to the target inputs; focused tests assert company-name and approved-context changes alter it (`tests/unit/test_cover_letter_preview.py:121-131`).

Canonical preview serialization uses UTF-8-oriented JSON settings: Unicode retained, keys sorted, compact separators, and non-finite JSON numbers forbidden (`services/documents/cover_letter_preview.py:46-52`). List order remains significant. Attributes outside the five selected keys do not affect this fingerprint.

Invalid/missing IDs and supported serialization/fingerprint failures produce `PreparationError` with code `COVER_LETTER_PREVIEW_JOB_REQUIRED` (`services/documents/cover_letter_preview.py:24-25,27-50`). The caught exceptions are exactly `KeyError`, `TypeError`, `ValueError`, and `OverflowError` inside snapshot construction/serialization. Other unusual failures, including UTF-8 encoding failures after serialization, are not translated by this helper.

Fingerprinting is a staleness/integrity comparison, not authentication, authorization, or proof that job contents are truthful.

## Career-fact snapshot binding

**Source:** `services/documents/cover_letter_preview.py:63-88,133-150`

`build_cover_letter_preview` accepts a supplied fact list only when it is bound to the current profile:

- If `facts_profile_id` is explicitly supplied, it must be an exact string equal to `profile['id']`.
- If omitted, the function accepts facts only when `facts is profile.get('facts')` by Python object identity; a detached/copy list must carry the explicit profile ID.
- Missing, non-string, or foreign identity fails with `COVER_LETTER_PREVIEW_EVIDENCE_INVALID` (`services/documents/cover_letter_preview.py:139-150`).

Both the supplied facts and the profile's current facts must be lists of at most 1,000 rows. The nested `index_rows` helper requires each row to be a dictionary and derives identity from truthy `fact_id`, otherwise truthy `id`. The chosen ID must be an exact nonblank string no longer than 128 characters and unique within that list (`services/documents/cover_letter_preview.py:63-82`). IDs are not trimmed or case-normalized; equality is exact.

The supplied and current maps must have identical key sets. Each corresponding row must produce the same `source_fact_fingerprint`, which hashes only `fact_id`, `kind`, `text`, `source`, `confidence`, `verified`, and `user_approved` (`services/documents/cover_letter_preview.py:81-88`; `services/documents/tailor.py:188-192`). Extra non-fingerprinted fact fields do not participate in this equality check.

Mismatch, duplication, unsupported list/row shape, excessive row count, or changed fingerprint uses `COVER_LETTER_PREVIEW_EVIDENCE_INVALID`. Ordinary exceptions from the delegated fingerprint helper are not caught here. Focused tests cover detached/foreign identity, text tampering, duplicate IDs, and approval tampering (`tests/unit/test_cover_letter_preview.py:109-119,151-161`; `tests/unit/test_document_fact_trace_sustain_270.py:132-156`).

## `build_cover_letter_preview(...) -> dict`

```python
build_cover_letter_preview(
    profile,
    job,
    facts,
    *,
    expected_profile_version,
    expected_job_fingerprint,
    facts_profile_id=<identity-bound default>,
    style="professional",
    length="normal",
)
```

**Source:** `services/documents/cover_letter_preview.py:116-284`

### Profile and job preconditions

The profile must be a dictionary with a nonblank string `id`. `expected_profile_version` must be exactly 64 lowercase hexadecimal characters and equal `profile['version']` (`services/documents/cover_letter_preview.py:133-138`). Invalid profile shape/version format produces `COVER_LETTER_PREVIEW_PROFILE_REQUIRED`; an exact-version mismatch produces `COVER_LETTER_PREVIEW_PROFILE_STALE`.

The current job snapshot is recomputed. `expected_job_fingerprint` must be the same lowercase SHA-256 shape and equal that recomputed value, or the function raises `COVER_LETTER_PREVIEW_JOB_STALE` (`services/documents/cover_letter_preview.py:152-156`). Use `cover_letter_preview_fingerprint` on the same current saved job snapshot; do not reuse the narrower target fingerprint.

After staleness checks, job evidence requires:

- nonblank string `title` and `company`;
- string `description` (it may be empty here, but the delegated builder still requires usable responsibility evidence);
- a nonempty list of nonblank string skills, each at most 100 characters;
- optional `responsibility_evidence` as a list of nonblank strings;
- optional `attributes.required_skills` / `preferred_skills` as lists of nonblank strings (`services/documents/cover_letter_preview.py:91-113`).

Unsupported job evidence uses `COVER_LETTER_JOB_CONTEXT_MISSING`. This validator does not impose explicit list-count or responsibility-string-length limits; downstream deterministic selection bounds what is used.

### Deterministic draft selection

The service calls `build_cover_letter(profile, job, facts, style=..., length=...)` with the indexed supplied fact rows (`services/documents/cover_letter_preview.py:159-167`). Supported styles are professional, warm, technical, direct, conversational, and formal. Supported lengths are very short, concise, normal, and detailed (`services/documents/cover_letter.py:9-17,150-155`). The delegated normalizer case-folds, trims/collapses whitespace, and changes underscores to spaces; the HTTP request model is stricter and accepts only exact literals (`services/documents/cover_letter.py:100-102`; `apps/api/cover_letter_preview.py:28-32`).

The builder derives responsibilities from saved explicit evidence or the saved description, chooses approved/source-backed facts with confidence at least 80 that overlap target skill/responsibility tokens, and returns deterministic paragraphs/context (`services/documents/cover_letter.py:157-197,218-262`). Missing job context produces `COVER_LETTER_JOB_CONTEXT_MISSING`; unsupported style/length produces `COVER_LETTER_OPTIONS_INVALID`; no relevant eligible facts produces `COVER_LETTER_RELEVANT_FACTS_MISSING`.

A delegated `PreparationError` is re-raised unchanged. Delegated `AttributeError`, `KeyError`, `TypeError`, or `ValueError` is translated to `COVER_LETTER_PREVIEW_EVIDENCE_INVALID` (`services/documents/cover_letter_preview.py:160-167`). Exceptions after that call, from unexpectedly malformed trusted context, are not wrapped by this block.

### Selected-fact eligibility and trace

The returned context must contain a nonempty fact-ID list and a matched-terms dictionary or the service raises `COVER_LETTER_RELEVANT_FACTS_MISSING` (`services/documents/cover_letter_preview.py:169-172`). Every selected fact is rechecked for:

- current presence by exact ID;
- `verified is True` and `user_approved is True`;
- numeric confidence that does not compare less than 80;
- nonblank string source and text (`services/documents/cover_letter_preview.py:174-182`).

An ineligible selected fact raises `COVER_LETTER_PREVIEW_EVIDENCE_INVALID`. The service records per-fact SHA-256, kind, source, approval flags, confidence, and matched terms—but deliberately omits raw fact text from `source_facts` (`services/documents/cover_letter_preview.py:183-194`). The sustained trace test checks deterministic serialization and absence of private raw-text markers (`tests/unit/test_document_fact_trace_sustain_270.py:93-130`).

This eligibility check rejects numeric confidence below 80 but does not independently impose an upper bound or finiteness check. Upstream profile validation remains a separate boundary.

### Company context

The delegated company-context helper returns context only when it is user-provided/unverified, explicitly reviewed and approved, source-identity verified by an allowed method, company-scoped, bounded, URL/domain-consistent, and content-hash consistent (`services/documents/cover_letter.py:55-97`). Invalid or absent company context is omitted rather than failing the preview.

When included, the preview returns only the allowlisted fields plus:

- `included: True`;
- an explicit `USER_PROVIDED / CONTENT UNVERIFIED` caveat;
- compact `company_context_evidence` containing inclusion status, content hash, version, and caveat (`services/documents/cover_letter_preview.py:196-216`).

When omitted, `company_context` is `None`, evidence status is `OMITTED_UNVERIFIED`, and the caveat explains why such content is absent (`services/documents/cover_letter_preview.py:217-226`). Focused tests verify both paths (`tests/unit/test_cover_letter_preview.py:94-98,163-186`).

### Job evidence and result bounds

The service normalizes invalid/missing company identity to `SOURCE_LISTED`, preserves selected skills/responsibilities from the delegated context, derives skill strengths, and records whether responsibilities came from explicit saved evidence or parsed description (`services/documents/cover_letter_preview.py:228-253`). The skill-source label is a nonblank saved `attributes.skill_evidence` truncated to 128 characters, otherwise `SAVED_JOB_SKILLS_UNCLASSIFIED`.

Paragraphs join with blank lines. Empty/whitespace-only output or text over 20,000 characters raises `COVER_LETTER_PREVIEW_RESULT_INVALID` (`services/documents/cover_letter_preview.py:255-257`).

### Output shape

A successful result contains (`services/documents/cover_letter_preview.py:259-284`):

```text
schema_version, status="READY", preview_text,
profile_id, profile_version,
job_id, job_fingerprint,
fact_ids, source_fact_ids,
source_fact_fingerprints,
source_facts,
matched_terms,
responsibilities, skills, skill_strengths,
company_identity,
company_context,
company_context_evidence,
company_context_caveat,
job_evidence,
style, length,
read_only=True,
document_created=False
```

The two fact-ID arrays contain the selected IDs. `source_fact_fingerprints` maps each selected ID to its source snapshot hash. `matched_terms` maps each selected ID to copied matched terms. `job_evidence` contains skill term/strength rows, skill source, responsibilities, and responsibility source.

`read_only` and `document_created` are truthful result markers for this function; they are not authorization to skip caller-side access control or current-snapshot checks.

## Error summary

All stable failures below are `PreparationError(code, message)`, a `ValueError` subclass carrying `.code` (`services/documents/errors.py:1-7`):

| Code | Source-level condition |
|---|---|
| `COVER_LETTER_PREVIEW_PROFILE_REQUIRED` | invalid profile ID or expected version format |
| `COVER_LETTER_PREVIEW_PROFILE_STALE` | current profile version differs |
| `COVER_LETTER_PREVIEW_JOB_REQUIRED` | missing/unsupported fingerprintable job snapshot |
| `COVER_LETTER_PREVIEW_JOB_STALE` | expected fingerprint invalid or differs |
| `COVER_LETTER_PREVIEW_EVIDENCE_INVALID` | fact identity/snapshot mismatch, ineligible selected fact, or mapped builder data error |
| `COVER_LETTER_OPTIONS_INVALID` | unsupported style or length |
| `COVER_LETTER_JOB_CONTEXT_MISSING` | insufficient saved title/company/skill/responsibility evidence |
| `COVER_LETTER_RELEVANT_FACTS_MISSING` | no approved relevant source fact or invalid empty selection context |
| `COVER_LETTER_PREVIEW_RESULT_INVALID` | empty or over-20,000-character preview |

The API maps known service codes to 409/422, looks up the requested profile and a job by job ID, and adds `COVER_LETTER_PREVIEW_CONTEXT_STALE` if its post-render re-read changed (`apps/api/cover_letter_preview.py:16-25,35-95`). That route behavior is not implemented by the pure service itself.

## Safe illustrative example (not executed)

This synthetic, in-memory example was **not executed** while preparing this reference:

```python
from services.documents.cover_letter_preview import (
    build_cover_letter_preview,
    cover_letter_preview_fingerprint,
)

profile = {
    "id": "synthetic-profile",
    "version": "a" * 64,
    "name": "Synthetic Candidate",
    "facts": [{
        "fact_id": "python",
        "kind": "skill",
        "text": "Built Python reporting services for internal teams.",
        "source": "synthetic reviewed resume",
        "confidence": 95,
        "verified": True,
        "user_approved": True,
    }],
}
job = {
    "id": "synthetic-job",
    "company_id": "synthetic-company",
    "company": "Example Systems",
    "title": "Platform Engineer",
    "skills": ["Python"],
    "description": "Responsibilities:\nBuild Python reporting services.",
    "attributes": {
        "skill_evidence": "structured_required_skills",
        "required_skills": ["Python"],
        "preferred_skills": [],
        "company_identity": "SOURCE_LISTED",
    },
}
preview = build_cover_letter_preview(
    profile,
    job,
    profile["facts"],
    expected_profile_version=profile["version"],
    expected_job_fingerprint=cover_letter_preview_fingerprint(job),
    facts_profile_id=profile["id"],
    style="professional",
    length="concise",
)
assert preview["read_only"] is True
assert preview["document_created"] is False
```

This performs deterministic computation only. It does not persist the preview, create a document/application, call AI, access a live employer, or submit anything.

## Maintainer cautions

1. Use the full preview fingerprint, not the narrower target fingerprint, for stale-job comparison (`services/documents/cover_letter_preview.py:22-60`).
2. Preserve fact-list profile identity and exact current-snapshot fingerprint comparison; equality of IDs alone is insufficient (`services/documents/cover_letter_preview.py:63-88,139-150`).
3. Keep raw fact text out of the compact `source_facts` provenance trace; `preview_text` necessarily contains selected factual wording.
4. Invalid company context must remain omitted/caveated, never silently promoted to verified company fact (`services/documents/cover_letter_preview.py:196-226`).
5. The pure service validates its supplied snapshots only once. Keep the API's post-render profile/job re-read if database state can change concurrently (`apps/api/cover_letter_preview.py:79-94`).
6. Do not treat a preview as a durable document, an application submission, or permission to use it externally.
7. Historical PASS/PARTIAL records and inspected tests are not current execution evidence; runtime verification remains **NOT_RUN**.

## References inspected at the exact commit

- `services/documents/cover_letter_preview.py:1-284`
- `tests/unit/test_cover_letter_preview.py:1-187`
- `tests/unit/test_document_fact_trace_sustain_270.py:72-156`
- `tests/integration/test_cover_letter_preview_api.py:20-154`
- `apps/api/cover_letter_preview.py:16-95`
- `services/documents/cover_letter.py:9-17,55-102,150-262`
- `services/documents/tailor.py:176-192,201-210`
- `services/documents/errors.py:1-7`
- `services/documents/AREA.md:1-14`
- `docs/spec/areas/career_profile_documents.md:267-315`
- `docs/evidence/2026-10-12-cover-letter-browser-contracts.json:1-79` (historical evidence only; not rerun)
