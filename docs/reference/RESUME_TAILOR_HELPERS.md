# Maintainer reference: `services/documents/tailor.py`

## Source identity and scope

- **Source commit:** `2f01b876762fcbdd70f11e69865a48257503af93`
- **Source path:** `services/documents/tailor.py`
- **Full source blob SHA:** `7cefa857851a0184343b600b79ecae9916ebdc1a`
- **Source extent:** lines 1–363 at that commit.
- **Evidence status:** static maintainer documentation derived from the immutable source and nearby exact-commit tests. No tests, CI, document generation, or runtime providers were executed for this draft.

`tailor.py` is a deterministic, in-memory policy/helper module. It filters approved facts, validates user-reviewed rewrites, selects target-relevant summaries and bullets, orders skill facts, and emits fingerprints/context used to detect stale or altered document history. It does not itself read a database, write files, call a network service, render documents, or submit applications (`tailor.py:1-8,30-363`).

## Existing documentation check

`services/documents/AREA.md` already documents the document subsystem's factual-only boundary, `prepare(...)` interface, immutable provenance, and no-invented-qualification rule (`AREA.md:1-25`). It also documents stable skill-slot ordering and recorded generation context (`AREA.md:56-67`) and immutable resume comparison (`AREA.md:117-130`). Those topics are **no_new_gap** here and are not duplicated as architecture prose. However, its statement that broader summary adjustment and factual rephrasing are not implemented (`AREA.md:68-69`) is stale or narrower than the reviewed-rewrite contract implemented by this pinned source and its API consumer; maintainers should not use those two lines to deny the functionality documented below (`tailor.py:50-151`; `apps/api/resume_rewrite_review.py:13,145-160`).

The useful gap is narrower: the exact commit contains 11 public functions without docstrings. The source and tests expose their contracts, but the area guide does not enumerate their required dictionary shapes, tuple/map outputs, direct exceptions, or fingerprint composition.

## Actual entry points and call graph

The stateful product entry point is `services.documents.generate.prepare(...)`, not this helper module. `prepare` validates top-level flags, locks/loads the profile, obtains eligible rows, applies selection and reviewed rewrites, then later performs summary/bullet selection, skill ordering, and emphasis (`services/documents/generate.py:456-522,721-726`).

Other direct consumers are:

- `alignment_preview.build_alignment_preview`, which validates snapshot pins before calling eligibility and match helpers (`services/documents/alignment_preview.py:9-56`).
- `comparison`, which rebuilds saved rewrites and verifies selection/fingerprint/context before presenting history (`services/documents/comparison.py:55-125`).
- `cover_letter_preview`, which uses target/source fingerprints and rewrite policy (see reference list).
- `apps.api.resume_rewrite_review`, which imports `target_fingerprint` directly and uses it after building a rewrite-review preview to re-read and revalidate the current job snapshot before returning (`apps/api/resume_rewrite_review.py:13,145-160`).

Internal dependencies are explicit:

- Skill canonicalization and mention extraction: `canonical_skill_id`, `extract_skill_mentions` (`tailor.py:5,250-267`).
- Template/model aliases: `TAILORED_TEMPLATE`, `TAILORED_MODEL` exported locally as `TEMPLATE`, `MODEL` (`tailor.py:7`).
- Stable document error type: `PreparationError` (`tailor.py:8`; `services/documents/errors.py:1-7`).
- Date/number preservation tokens are imported lazily from document validation (`tailor.py:61,77-84`).
- Responsibility extraction delegates to `cover_letter._responsibilities` before conservative normalization (`tailor.py:201-216`).

## Input shapes

### Fact rows

Different helpers require different subsets; callers should not assume arbitrary mappings are accepted.

- Eligibility requires `verified`, `user_approved`, numeric `confidence`, and string `source`; rows pass only when both booleans are truthy, confidence is at least 80, and stripped source is nonempty (`tailor.py:154-156`).
- Selection/matching additionally uses `fact_id`, `kind`, and `text` (`tailor.py:159-169,219-273`).
- Rewrite validation requires eligible rows and uses `fact_id`, `kind`, and `text`; supported v2 rewrite kinds are `summary`, `responsibility`, `experience`, `accomplishment`, `project`, and `technology_experience` (`tailor.py:10-13,50-98,107-110`).

### Job mappings

- Matching uses `job['skills']`, a sequence whose elements are treated as strings (`tailor.py:250-267`).
- Skill ordering context also requires `job['id']` and `job['title']` (`tailor.py:353-363`).
- Target fingerprints require `id`, `title`, and `skills`; `description` is optional and converted with `str(job.get('description') or '')` (`tailor.py:176-185`).
- Responsibility-based selection reads optional `description` through the cover-letter responsibility parser (`tailor.py:201-216,276-350`).

## The 11 undocumented public helpers

### 1. `eligible_rows(profile) -> list[dict]` — lines 154–156

Returns a new list containing the original fact row objects that meet all four approval gates: verified, user approved, confidence ≥ 80, and nonempty stripped source. It directly indexes `profile['facts']` and required row keys; malformed mappings are not normalized here.

### 2. `select_resume_rows(eligible, tailor_resume, requested) -> list[dict]` — lines 159–169

- `requested is None`: returns `eligible` unchanged.
- Explicit selection requires `tailor_resume` truthy and a list of 1–1000 unique, nonblank strings, each at most 128 characters.
- Every requested ID must exist in the eligible rows.
- Output preserves the original eligible-row order, not request order.
- Invalid input raises `PreparationError` with code `DOCUMENT_SELECTION_INVALID`.

### 3. `selection_fingerprint(identifiers) -> str` — lines 172–173

Returns lowercase SHA-256 hex over UTF-8 JSON using `ensure_ascii=False` and compact separators. Identifier order is significant because the JSON list is not sorted. This is a deterministic change-detection token, not a confidentiality boundary.

### 4. `target_fingerprint(job) -> str` — lines 176–179

Builds the target snapshot fingerprint from job ID, title, skills, SHA-256 of the stringified description, and normalized target responsibilities. Changes to any of those inputs can change the result.

### 5. `target_snapshot_fingerprint(job_id, title, skills, description_sha256, responsibilities=None) -> str` — lines 182–185

Returns lowercase SHA-256 hex over a JSON object containing exactly those five fields, encoded with `ensure_ascii=False` and sorted keys. Passing no responsibilities records JSON `null`, which is distinct from an empty list.

### 6. `source_fact_fingerprint(row) -> str` — lines 188–192

Hashes a snapshot of exactly: `fact_id`, `kind`, `text`, `source`, `confidence`, `verified`, and `user_approved`. It uses `row.get`, so a missing tracked field contributes `null`. Other row fields do not participate.

### 7. `matched_responsibility_phrases(facts, phrases) -> dict[str, list[str]]` — lines 219–226

Tokenizes each fact's text to lowercase alphanumeric/`+ # .` terms, rejoins them with single spaces, and returns a map for every fact ID. A phrase matches only at the helper's explicit word-like boundaries; it does not perform semantic or fuzzy matching.

### 8. `bullet_selection(original_ids, skill_matches, responsibility_matches) -> tuple[list[str], str, dict]` — lines 229–247

Selects IDs with either skill or responsibility evidence, preserving `original_ids` order. If neither evidence class matches anything, it falls back to all IDs. The mode is one of:

- `EXACT_TARGET_SKILL_AND_RESPONSIBILITY_PHRASES`
- `EXACT_TARGET_SKILL_BULLET_FACTS`
- `EXACT_TARGET_RESPONSIBILITY_PHRASE_BULLET_FACTS`
- `ALL_ELIGIBLE_BULLET_FACTS`

The reasons map contains `EXACT_TARGET_SKILL`, `EXACT_TARGET_RESPONSIBILITY_PHRASE`, or `ALL_ELIGIBLE_FALLBACK`. Both input match maps are directly indexed for every ID.

### 9. `matched_labels(facts, job) -> dict[str, list[str]]` — lines 250–267

Deduplicates target labels by canonical skill ID when available, otherwise by case-folded text. Fact mentions come from `extract_skill_mentions`, plus canonicalization of the fact's whole text. Canonical targets match by ID; unknown targets use escaped, case-insensitive exact-boundary regex matching. The returned labels retain the first target spelling/order.

### 10. `emphasis(facts, job, include_responsibilities=False) -> list[str]` — lines 270–273

Returns matching fact IDs in input order. By default it considers only `skill` and `technology_experience`; when enabled, it also considers `responsibility`. It delegates match semantics to `matched_labels` and does not rewrite text.

### 11. `order_skills(facts, job, include_responsibilities=False) -> tuple[list[dict], dict]` — lines 353–363

Reorders only rows whose kind is `skill`, placing them back into the original skill slots after a stable descending sort by number of matched target labels. Every non-skill row remains in its original position. The context records mode, job identity/title/skills, target fingerprint, original and ordered IDs, and per-fact matched labels. Equal relevance retains source order because Python sorting is stable (`tailor.py:357-363`; specified by `tests/unit/test_skill_order.py:97-115`).

## Other public helpers with source docstrings

These are already locally documented, but their operational contracts matter when maintaining the undocumented helpers:

- `validate_user_rephrases` is the v1 responsibility-only wrapper (`tailor.py:101-104`).
- `validate_user_rewrites` is the v2 wrapper for all supported rewrite kinds (`tailor.py:107-110`).
- `apply_saved_user_rephrases` and `apply_saved_user_rewrites` reconstruct historical reviewed wording and verify source bindings (`tailor.py:113-151`).
- `target_responsibilities` keeps only conservative normalized duty phrases with at least five total and three informative tokens (`tailor.py:201-216`).
- `select_summary_rows` and `select_bullet_rows` return `(rows, context)` with exact selection evidence and fingerprints (`tailor.py:276-350`). With no summary match, all summary rows remain; the source still records mode `EXACT_TARGET_SKILL_SUMMARY_FACTS` (`tailor.py:295-307`). With no bullet match, all eligible bullet facts remain via `ALL_ELIGIBLE_BULLET_FACTS` (`tailor.py:229-247,323-350`).

## Rewrite outputs and failure behavior

`validate_user_rewrites(facts, selected_fact_ids, rewrites)` returns `(copied_rows, provenance)` (`tailor.py:50-110`). A successful provenance record contains `fact_id`, `fact_kind`, the original text SHA-256, reviewed replacement text, `reviewed=True`, and policy `USER_CONFIRMED_FACTUAL_PARAPHRASE` (`tailor.py:85-95`). Input facts are copied rather than modified (`tailor.py:98`).

Explicit stable failures are:

| Code | Proven condition | Source |
|---|---|---|
| `RESUME_REPHRASE_INVALID` | malformed collection/record, duplicate fact rewrite, unchanged/empty/oversized/NUL text | `tailor.py:50-56,63-66,75-76,96-98` |
| `RESUME_REPHRASE_FACT_INVALID` | fact is missing, unselected, ineligible, or has unsupported kind | `tailor.py:57-71` |
| `RESUME_REPHRASE_REVIEW_REQUIRED` | replacement was not explicitly reviewed | `tailor.py:73-74` |
| `RESUME_REPHRASE_FACTS_CHANGED` | dates/numbers differ or a conservative named source token is absent | `tailor.py:77-84` |
| `RESUME_REPHRASE_CONTEXT_INVALID` | saved v1/v2 provenance has the wrong shape/policy/kind | `tailor.py:113-145` |
| `RESUME_REPHRASE_SOURCE_CHANGED` | saved source hash or fact kind no longer matches reconstructed provenance | `tailor.py:126-150` |
| `DOCUMENT_SELECTION_INVALID` | explicit resume selection violates enablement, type, size, uniqueness, or eligibility rules | `tailor.py:159-169` |

`PreparationError` subclasses `ValueError`, keeps the public message as the exception text, and stores the stable code on `.code` (`services/documents/errors.py:1-7`). The other helpers do not install a catch-all validation layer; direct indexing/string operations mean malformed shapes can raise ordinary Python exceptions. Product callers such as alignment preview add their own snapshot/type validation and translate unsupported job snapshots (`services/documents/alignment_preview.py:17-35`).

## Safe examples (pure, no I/O)

### Filter, select, and order approved facts

```python
from services.documents import tailor

profile = {"facts": [
    {"fact_id": "python", "kind": "skill", "text": "Python",
     "verified": True, "user_approved": True, "confidence": 95,
     "source": "User verified"},
    {"fact_id": "sql", "kind": "skill", "text": "SQL",
     "verified": True, "user_approved": True, "confidence": 90,
     "source": "User verified"},
    {"fact_id": "draft", "kind": "skill", "text": "Rust",
     "verified": False, "user_approved": True, "confidence": 95,
     "source": "User entered"},
]}
job = {"id": "job-1", "title": "Data Engineer", "skills": ["SQL"],
       "description": ""}

eligible = tailor.eligible_rows(profile)          # python, sql; not draft
selected = tailor.select_resume_rows(
    eligible, tailor_resume=True, requested=["python", "sql"]
)
ordered, context = tailor.order_skills(selected, job)
# `ordered` retains only the approved source rows; only skill slots can reorder.
# Inspect `context["matched_skills"]` and `context["ordered_fact_ids"]` rather
# than treating ordering as proof of a qualification.
```

### Handle a selection failure by stable code

```python
from services.documents.errors import PreparationError
from services.documents.tailor import select_resume_rows

try:
    select_resume_rows(eligible, tailor_resume=False, requested=["python"])
except PreparationError as error:
    assert error.code == "DOCUMENT_SELECTION_INVALID"
```

### Validate a user-reviewed factual paraphrase

```python
from services.documents.tailor import validate_user_rewrites

facts = [{
    "fact_id": "experience", "kind": "experience",
    "text": "Supported 12 clinics with Linux systems from 2020-2024.",
    "verified": True, "user_approved": True, "confidence": 95,
    "source": "User verified",
}]
rewritten, provenance = validate_user_rewrites(
    facts,
    ["experience"],
    [{"fact_id": "experience",
      "text": "Maintained Linux systems for 12 clinics from 2020-2024.",
      "reviewed": True}],
)
```

This example demonstrates exact date/number preservation plus immutable source binding (`tests/unit/test_resume_comparison.py:182-232`). Although both strings happen to retain `Linux`, plain `Linux` is **not** guaranteed to be a protected named token by `_NAMED_FACT_TOKEN`; that pattern targets acronym/mixed-case/suffixed forms, with separate issuer handling around credential context (`tailor.py:14-47`). Do not cite this example as proof that arbitrary proper product or operating-system words are enforced.

Named credential/issuer preservation is specified separately by the exact-commit table-driven cases for `Cisco CCNA`, `CompTIA Security+`, and `Microsoft Azure Administrator Associate`: retaining those tokens is accepted, while dropping the credential or issuer raises `RESUME_REPHRASE_FACTS_CHANGED` (`tests/unit/test_resume_tailoring_facts.py:8-72`). These tests were read, not executed, for this draft.

## Maintainer invariants

1. **Never broaden eligibility silently.** Eligibility is the source boundary for generation and preview (`tailor.py:154-156`; `generate.py:500-514`; `alignment_preview.py:37-53`).
2. **Preserve source order unless a function explicitly says otherwise.** Selection preserves fact order; only skill rows move within skill slots (`tailor.py:159-169,353-363`).
3. **Do not reinterpret fingerprints as secrets.** They bind exact snapshots/history and are rechecked by consumers (`comparison.py:94-125`); source strings can still be sensitive.
4. **Fallback means retention, not inferred relevance.** No summary/bullet match keeps eligible rows rather than manufacturing a match (`tailor.py:229-247,276-350`; `tests/unit/test_skill_order.py:28-37,62-75`).
5. **Reviewed rewriting is not free-form generation.** Exact date/number multisets and conservative named tokens must survive, and saved provenance must remain bound to original source text (`tailor.py:50-151`).
6. **Keep context schemas synchronized with comparison.** Changes to modes, IDs, match maps, or fingerprints require corresponding comparison/history handling (`comparison.py:94-125`).

## Nearby exact-commit specifications (not executed here)

- `tests/unit/test_skill_order.py:12-125` — summary selection, responsibility exactness, skill-slot stability, aliases/boundaries, and emphasis.
- `tests/integration/test_resume_selection.py:23-98` — eligible selection, invalid/ineligible rejection, persistence/version behavior, and worker delivery.
- `tests/unit/test_resume_comparison.py:182-338` — reviewed rewrite provenance, stale source rejection, supported kinds, and number/date preservation.
- `tests/unit/test_resume_tailoring_facts.py:8-72` — named credential/issuer preservation.
- `apps/api/resume_rewrite_review.py:13,145-160` — exact API consumer import and post-preview target-fingerprint revalidation.
- `services/documents/AREA.md:1-25,56-70,117-130` — existing subsystem boundary, skill tailoring, and comparison documentation; lines 68-69 are stale or narrower than the implemented reviewed-rewrite contract.
