# Source metadata v1

MJ-REF-036 / [issue #406](https://github.com/MulattoTech/MJobs/issues/406).

MJobs exposes a provider-neutral, versioned projection of source-specific job
metadata. New nested public metadata can pass through the existing posting
storage and export without an adapter, database schema, or unrelated-adapter
change. Raw provider payloads and their historical snapshots remain unchanged.

This slice covers source-extension projection and export. Per-canonical-field
provenance and confidence remain a separate implementation and acceptance gate;
this change does not complete the whole MJ-REF-036 requirement.

## Read the metadata

The existing job-detail response, `GET /jobs/{job_id}`, adds `source_metadata`
to each item in `postings`. Metadata remains attached to its own posting. It is
not merged into canonical title, salary, company identity, application routes,
or source authority. Job-list responses do not acquire raw-payload copies.

```json
{
  "schema_version": 1,
  "status": "COMPLETE",
  "source": {
    "posting_id": "posting-one",
    "source_job_id": "native-123",
    "source_identifier": "public-board",
    "source_url": "https://jobs.example.test/feed",
    "fetched_at": "2026-11-09T03:00:00+00:00"
  },
  "extensions": {
    "new_vendor_field": {
      "industry": "Technology",
      "rating": 0,
      "enabled": false,
      "optional": null
    }
  },
  "omissions": {"private_fields": 0, "unsafe_urls": 0, "source_identity_fields": 0},
  "reason_codes": []
}
```

`source` comes from the durable posting record. Unknown source attributes remain
in the `extensions` namespace. A source payload containing its own `id`,
`schema_version`, `source`, `authority`, or `confidence` cannot overwrite the
envelope or grant authority. Metadata values are untrusted data; clients must
render them as text rather than execute markup or follow links automatically.

The envelope is generated when the posting is read. The unchanged raw posting
and raw snapshots remain the durable data authority; this is not a separately
stored historical normalization revision. No new import API or automatic
canonical-field promotion is introduced.

## Projection status and fidelity

| Status | Meaning |
| --- | --- |
| `COMPLETE` | All JSON metadata was retained under the v1 structural and omission policy. This is not a factual verification, trust, or requirement-acceptance result. |
| `PARTIAL` | Private-key fields or unsafe URL-bearing values were omitted. Counts and fixed reason codes explain the loss. |
| `UNAVAILABLE` | The payload was not an admissible JSON object or exceeded a limit. No truncated prefix is returned; the original raw record remains unchanged. |

Admissible unknown keys, nested objects/lists, Unicode, empty strings/collections,
zero, `false`, and `null` retain their values. A returned envelope is detached
from the original raw object. Editing the returned metadata cannot mutate raw
storage. Projection has no clock-dependent values and repeated reads of the
same stored posting are deterministic.

The policy conservatively omits credential, session, browser, prompt, diagnostic,
request/response, profile/contact, and related reserved key names at any depth.
It also omits values and object keys containing recognized unsafe links, including
links in prose or HTML. An unsafe object key omits its entire entry and counts as
one unsafe-URL omission; eligible public URL keys remain unchanged. Links with
credentials, query strings, fragments, sensitive path segments, unsupported
schemes, or malformed authorities are not exported as
partial links. HTML entities are decoded only for inspection; retained text is
unchanged. An omitted URL in an array becomes `null` to preserve array positions,
and `UNSAFE_URLS_OMITTED` makes that loss explicit.

These are conservative structural controls, not a universal secret or personal
information detector. Arbitrary sensitive prose in an otherwise admissible
unknown field cannot be reliably identified by key or URL checks. The projection
does not assert that source-provided data is factually correct or independently
verified. Source endpoint omissions also make the envelope `PARTIAL`. Invalid,
oversized, or unsafe-URI-bearing source identity fields are omitted with a
`source_identity_fields` count and `SOURCE_IDENTITY_OMITTED`. Genuinely missing
optional identities are distinct from rejected nonempty values.

The v1 limits are:

- 256 KiB of encoded metadata per posting.
- 64 KiB of UTF-8 data per string and 256 bytes per key.
- 512 items per object or list, 4,096 traversed keys/values, and 12 nested levels.
- Finite floating-point values, string object keys, and bounded JSON integers.

Unsupported values and limit failures produce fixed codes such as
`INVALID_METADATA_VALUE`, `INVALID_METADATA_KEY`,
`SOURCE_METADATA_NOT_OBJECT`, or `METADATA_LIMIT_EXCEEDED`. They do not expose
parser diagnostics or change canonical job reads. The URL scanner has a bounded
scheme prefix so ordinary long URL-free text does not trigger quadratic scans.

## Download a profile-scoped metadata snapshot

`GET /profiles/{profile_id}/exports/source-metadata.json` returns a JSON attachment
named `mjobs-source-metadata.json`, with `Cache-Control: no-store`.

```json
{
  "schema_version": 1,
  "export_type": "source_metadata",
  "profile_id": "example-profile",
  "postings": [
    {"job_id": "job-one", "source_metadata": {"schema_version": 1}}
  ]
}
```

The abbreviated envelope above has the full shape shown earlier in actual
responses. The export includes only jobs associated with the selected profile
through saved matches or applications, using the existing export-scope union.
It reads profile existence, associations, and postings in one database snapshot.
Postings are streamed one at a time; the total is limited to 10,000 records and
5 MiB of output. An oversized export returns HTTP 413 with
`DATA_EXPORT_TOO_LARGE`; a missing profile returns HTTP 404. Invalid internal
response shapes and unexpected errors have fixed messages.

These are projection/output limits. Reading one already-stored raw JSON row still
requires the database driver to decode that individual row; this change does not
retroactively limit the size of historical storage.

Each posting has its own source identity and projection status. Unknown eligible
metadata survives the database round trip and this download. Omitted metadata
is explicitly incomplete rather than described as lossless. A source that adds
an eligible field does not require a change to another adapter or this export.

Existing CSV, profile JSON, and profile backup/restore schemas are unchanged.
This endpoint exports the projection only, without adding raw-payload,
profile-record, application-trace, or document-byte sections. Metadata uses the
selective omission policy above. It does not fetch providers, modify jobs, send
outreach, or submit applications.

## Targeted verification

```text
python -m pytest -q tests/unit/test_source_metadata.py tests/integration/test_job_source_metadata.py
python -m pytest -q tests/unit/test_job_normalization.py tests/unit/test_normalization_contract.py tests/integration/test_data_export_flow.py tests/integration/test_user_read_models.py tests/integration/test_remotive.py
```

The new tests cover unknown JSON fidelity, durable reopen, the mounted job-detail
and metadata-export APIs, source separation, matched/application-only profile
scope, explicit omissions, bounded failures, immutable raw/canonical data,
read-only SQL, no export network activity, and an existing Remotive adapter
collecting a new field through a synthetic transport. Executed revision-specific
results belong in the run evidence; this guide alone is not acceptance evidence.
