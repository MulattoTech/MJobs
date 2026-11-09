# Career-story JSON import (MJ-REF-002)

The mounted API accepts `POST /profiles/{profile_id}/career-stories/import` with
`Content-Type: application/json`. The request is a UTF-8 JSON document:

```json
{
  "schema_version": 1,
  "stories": [
    {
      "kind": "STAR_STORY",
      "title": "Improved report delivery",
      "text": "Situation: reports were slow. Task: improve delivery. Action: tuned queries. Result: reports arrived sooner.",
      "fact_ids": ["approved-fact-id"],
      "tags": ["reporting"],
      "allowed_uses": ["RESUME", "INTERVIEW_PREPARATION"]
    }
  ]
}
```

`kind` may be `STAR_STORY`, `ACCOMPLISHMENT`, or `RESUME_BULLET`.
Every story uses the exact fields and validation rules of the existing manual
story creation API. Fact IDs must belong to the target profile and currently be
verified, user approved, and sufficiently supported. The import cannot mark
stories or facts approved. Review each pending revision through the existing
story review route before it becomes reusable.

The document must contain exactly `schema_version` and `stories`, with 1–20
stories and at most 256 KiB of UTF-8 input. Duplicate JSON keys, non-finite
numbers, unknown fields, unsupported versions, and invalid fact links are
rejected. All records commit together; a failure leaves no partial batch.
Errors use the existing story API's `detail.code` response shape.

The response contains `canonical_sha256`, `created_count`, and a `stories`
array. Each story includes `created` and `import_origin` with the canonical
digest and zero-based record index. The digest hashes normalized story data,
not the uploaded byte stream: whitespace and JSON member order do not alter
it. Story titles and text follow existing story normalization, including
trimming surrounding whitespace. That original normalized text remains in
immutable revision 1. Reimporting the same normalized batch returns the
original rows without resetting review or appending revisions; an edit through
the existing route appends a new pending revision. Import provenance remains
visible in later listing and revision readback. A changed fact fingerprint
blocks replay rather than silently accepting a stale original.

This slice provides the mounted JSON API and existing list/review/edit consumer
flow. A browser file picker and connected browser acceptance are outside this
slice; MJ-REF-002 remains partial until the full issue acceptance is verified.
