# Web request boundary

MJobs combines an owner-controlled local API with the existing Cloudflare Access
deployment. The request-origin checks are a browser defense; they do not
authenticate a caller or establish permissions.

## Request handling

The API rejects duplicate Host, Origin, Referer or Sec-Fetch-Site headers with a
fixed 400 response before dispatch. A proxy and application must not authorize
different values from an ambiguous header list.

An explicit Origin must match the expected origin. When Origin is absent, an
explicit Referer is parsed and its scheme, hostname and effective port must
match. Paths and queries are not used as authorization evidence. Relative URLs,
userinfo, malformed ports, controls, whitespace, backslashes and fragments are
rejected. Default HTTP/HTTPS ports normalize to 80/443. Prefix and sibling-host
matches are never sufficient.

Explicit cross-site, same-site or invalid Sec-Fetch-Site values still deny
private reads and mutations. A favorable referrer or fetch signal cannot
override an explicit Origin denial. Public shell/assets and health reads remain
available under the existing policy; this does not exempt mutations.

For the configured public hostname, mutations additionally require an affirmative
same-origin Origin, validated referrer or Sec-Fetch-Site value. Cloudflare
terminates HTTPS, so the expected public origin is always HTTPS.

Native clients calling the public hostname must provide their normal
authentication and an explicit matching Origin header (for example,
Origin: https://mjobs.example.test when that is the configured hostname).
The header is not a credential and does not replace Cloudflare Access.
Existing headerless clients addressing a local hostname keep their API contract.

Rejected requests keep no-store, nosniff, no-referrer and the existing restrictive
Content-Security-Policy. Their responses do not echo header values or private
records.

## Validation error response privacy

Generic FastAPI 422 validation responses preserve a compact `detail` array
with coarse location structure, nonnegative list indexes and fixed error
categories. A dictionary key may itself contain an application answer or
credential; custom validator messages and error-type strings can also contain
rejected content. None is treated as safe response text. String location
components are redacted to `[field]`; up to 32 errors and six location
components per error are returned. Route-specific credential and AI-settings
errors continue to use their fixed error codes.

Clients must not rely on raw nested field names or free-form validator messages
in 422 responses. This guard applies to the common RequestValidationError
handler; it does not make arbitrary route-specific HTTPException detail or
ValueError text safe. Those remain separate review surfaces.

The browser job extraction and cheap prefilter preview routes also use fixed
messages for their handled service/validation failures. Their existing status
codes, machine-readable codes and `detail` shape are preserved, but exception
text is never included. Captured content, persisted search criteria and provider
diagnostics must not become response diagnostics. These guards do not add retry,
persistence or external requests. Synthetic fault-injection tests cover both
error classifications, bounded output, unchanged stored records and recovery.

## Security boundaries and limitations

Local clients with no browser-origin headers remain trusted by the existing
local API contract. Consequently this change does not prove protection against a
legacy browser or intermediary that supplies none of these headers. Local
machine access is not a multiuser authentication boundary.

Remote authentication, authorization, HTTPS and session-cookie attributes remain
the deployment's Cloudflare Access/tunnel responsibilities. An observed allow
policy, secure cookie flag or successful owner session alone does not establish
every policy's authorization behavior. The original MJ-REQ-172 requirement stays
incomplete until all its criteria have evidence.

The referrer fallback follows the origin-verification guidance in the
[OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#using-standard-headers-to-verify-origin).
The local headerless-client exception above is an explicit compatibility boundary.

## Verification

- Unit request-boundary tests cover malformed/foreign referrers, duplicate
  headers in both orders, explicit denial precedence, public mutation admission
  and preserved local native behavior.
- Persisted API tests prove rejected requests leave saved searches unchanged
  after closing and reopening the database.
- A controlled Chromium test preserves the real browser Referer while a
  test-only ASGI proxy strips Origin and fetch metadata. It asserts both the
  original browser metadata and the exact headers reaching the application.
  This is an injected proxy condition, not a claim that current Chromium
  naturally omits those headers.
- Existing private-read, error-header, CSP and structured-job-text regressions
  remain separate checks.

Run-specific source, review, failure and deployment receipts belong in the
containing PR and dated evidence, not in this general contract.
