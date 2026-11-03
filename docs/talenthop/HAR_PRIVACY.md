# Offline HAR privacy boundary

The standard-library command tools.verification.har_sanitize reads a local HAR and produces a deliberately lossy, schema-only HAR derivative plus a provenance manifest. It never browses, captures, replays requests, calls a model, or uploads artifacts.

## Usage

```text
python -m tools.verification.har_sanitize PRIVATE_CAPTURE.har --output reviewed.sanitized.json --manifest reviewed.manifest.json
python -m unittest tests.unit.test_har_sanitize tests.unit.test_talenthop_catalog
python -m tools.verification.talenthop_catalog
```

Use an existing private input directory and new output filenames. Both destinations must differ from the input and each other. Publication is exclusive: existing files are never overwritten; a failed second artifact publication removes only the output created by this attempt. Input bytes are unchanged. Filesystem durability across a physical power loss and protection against hostile concurrent path replacement are not claimed.

The input is capped at 16 MiB, 2,000 entries/pages, 256 KiB per body, 128 fields/items at each schema level, eight schema levels and 32 MiB serialized output. Errors printed by the CLI are fixed categories and contain no source values or private paths. Unknown methods become UNKNOWN, invalid status/timing values are unknown, and malformed/unsupported URLs become an opaque placeholder.

## What survives

Entry ordering, supported HTTP methods, bounded numeric status/timing/size fields, relative page associations and redirect relationships survive. Absolute wall-clock times are shifted to a fixed epoch. Hostnames, path segments and field names become encounter-order aliases; alias IDs are meaningful only within one derivative. Query values are replaced, and fragments/userinfo are removed. Different raw path segments remain different aliases; this is not a semantic endpoint-template inference.

JSON and URL-encoded bodies retain bounded, anonymized schema shapes only. Multipart parameter metadata retains anonymized field shape when HAR params exist. Raw multipart/text/HTML/binary/base64 content, unsupported encodings, oversized or malformed bodies are omitted with an explicit mode. Nested fields are anonymized, including fields whose names contain secrets. Array types are sampled within the bound and truncation is explicit.

Cookies, all headers, status text, IP addresses, filenames, page titles, initiators, comments, extension fields, source tool metadata and response bodies are not copied. Header-derived cache policy, WebSocket/streaming content, missing/cache-only body content and unrecorded interactions are therefore not established. A real capture may require additional private analysis to recover functional semantics.

The closed-representation validator rejects free-form strings, unexpected keys and nonempty headers/cookies/cache before publication. Regression cases seed dummy bearer/cookie/CSRF-like values, email/phone data, arbitrary URL/query keys, nested JSON, form/multipart/text and response headers. It is not a universal proof against covert channels or incorrectly populated HAR numeric metadata. A human context/privacy review remains required before sharing.

## Provenance and raw disposition

The manifest records input/output hashes, sizes, entry count, sanitizer version and policy. capture_status is always NOT_ATTESTED: the tool cannot prove that an input is a real capture, complete, authorized or free of omitted journeys. No raw file path or source hostname is included. privacy_review remains REQUIRED_BEFORE_SHARING.

Real raw captures must stay in approved private encrypted storage with an owner and short retention, or a protected temporary location for immediate analysis if no archive exists. This tool neither configures storage nor deletes raw inputs; it cannot promise secure erasure on SSDs or backups. Record the actual disposition separately, without public private-artifact URLs. The current sprint captured no raw HAR and therefore has no retention/archive or captured derivative to report.

Git ignores raw HAR extensions as an accidental-publication guard. This does not replace review, access controls or the existing secret scan, and can be bypassed by force-add. Public fixtures must be synthetic. The original acceptance authorities are not changed by this tooling.
