# Adding an application adapter

This guide covers MJ-REQ-284. Application adapters currently provide provider
route recognition and review-oriented form analysis. Public ATS uploads, live form
interaction and submission remain disabled. The local mock application flow and
controlled dry-run flow are separate capabilities with separate evidence.

## Entry points and declared capability

Implement provider-specific behavior under [services/applications/adapters](../services/applications/adapters/).
The [immutable registry](../services/applications/provider_registry.py) owns factory
selection and route/capability dispatch. [form_review_providers.py](../services/applications/form_review_providers.py)
projects analysis into the bounded API contract; [the form-review worker](../workers/application_form_review.py)
owns durable execution, current route/profile validation and persistence.

There is no universal live-submit adapter base class to implement. Use the current
call sites and a nearby adapter as the contract. The core provider surface is:

| Method | Responsibility |
| --- | --- |
| `capabilities()` | Return explicit booleans for behavior actually implemented; unknown support stays false |
| `normalize_route(route)` | Return validated provider/tenant/requisition/URL identity, or `None` |
| `analyze_form(html, route_url=...)` for extension adapters | Analyze supplied bounded markup locally; no fetch or browser launch |
| `requires_human_review(analysis)` | Fail closed for malformed, ambiguous, sensitive or unsupported fields |
| Provider-specific `analyze_page` / `validate_page` | Inspect a controlled loaded DOM where that provider implements it; preserve cancellation/deadline checks |

Built-ins use their existing specialized dispatch paths. Not every adapter supports
both static HTML and DOM inspection. For example, a provider that requires computed
visibility returns `DOM_INSPECTION_REQUIRED` from static analysis; a successful
route normalization does not upgrade that capability.

Registry extensions are immutable snapshots. They cannot replace a built-in or
reserved provider ID. A deployment-owned module may expose
`APPLICATION_ADAPTER_REGISTRY`, selected at process startup with
`MJOBS_EXTENSION_REGISTRY_MODULE`; this is trusted installed code, never a module
name accepted from an HTTP request, uploaded file or scraped page. API admission
and worker revalidation must use the same configured snapshot. See
[extension contract tests](../tests/unit/test_extension_point_contracts.py).

## ATS detection and route identity

[ATS detection](../services/ats_detection/detector.py) supplies observations and
confidence. The selected application adapter independently validates its route;
detected provider text is not permission to navigate or submit.

Keep an exact allowlist of supported HTTPS host/path shapes. Reject credentials,
unexpected ports, malformed identifiers, cross-provider paths and conflicting
caller-supplied tenant/requisition metadata. Normalize only transformations that
preserve provider identity. Preserve regional identities such as Lever's global
and EU origins. An unsupported path returns `None` rather than guessing a URL.

This example is offline and performs no provider interaction:

```python
from services.applications.provider_registry import capabilities, normalize_route

route = normalize_route("greenhouse", {
    "provider": "greenhouse",
    "url": "https://job-boards.greenhouse.io/example/jobs/1234",
})
assert route["tenant"] == "example"
assert route["requisition"] == "1234"
assert capabilities("greenhouse")["submission"] is False
assert normalize_route("greenhouse", {
    "provider": "greenhouse", "url": "https://unrelated.example/jobs/1234",
}) is None
```

## Navigation and durable ownership

Do not add public browser navigation to `normalize_route` or `analyze_form`.
External form review follows the existing worker and bounded read-only transport.
Local browser flows use the fixture origin and durable operation admission.
Revalidate the application, profile version, exact route, current worker claim and
review fingerprint before work and before persistence. A route or DOM change
requires review rather than continuation with an old plan.

For fixture browser tests, follow [mock_adapter.py](../services/applications/mock_adapter.py)
and [controlled_dry_run.py](../services/applications/controlled_dry_run.py): constrain
navigation and requests to the permitted local origin, use the shared browser
capacity and operation lifetime, and bound every wait by the remaining deadline.
Do not launch an unbounded browser pool or treat a browser timeout as proof that
an application was never sent.

## Field mapping and questions

[form_analysis.py](../services/applications/form_analysis.py) handles control
metadata and ambiguity. The API projection returns a value-free allowlist such
as safe `id`, `name`, `type`, `required` and `review_reason`, not entered values,
raw labels, arbitrary attributes, HTML or screenshots. Use fixed reason codes for
parser failures because exception text can include applicant information.

Map a question only to an approved career-profile fact or reviewed answer. Preserve
the [preflight](../services/applications/preflight.py) questionnaire fingerprint,
confidence, sensitivity and current-profile checks. Custom or sensitive prompts,
ambiguous labels, CAPTCHA/MFA, inaccessible controls and unrecognized dynamic
fields stop for review. A familiar field ID must not override a conflicting
visible question. Never synthesize credentials, qualifications or legal answers.

Multi-page and dynamic forms use [multipage_state.py](../services/applications/multipage_state.py)
and [dynamic_form_state.py](../services/applications/dynamic_form_state.py) to retain
review state. Do not flatten a changed questionnaire into the last page's answers
or silently reuse a prior review after the form fingerprint changes.

## Uploads and validation

Public `document_upload` is currently false. A provider's `mock_fixture_upload`
capability authorizes only its implemented local fixture helper. For example,
Greenhouse's helper accepts an HTTP loopback fixture, rechecks form analysis and
native validity, and requires the expected resume/cover-letter file controls.

Use [upload_integrity.py](../services/applications/upload_integrity.py) to create
verified immutable byte snapshots and attach files. Verify the actual selected
browser `File` bytes, size and SHA-256 against the document manifest; matching
filename alone is insufficient. Do not use caller-supplied arbitrary file paths or
change a generated document after its reviewed snapshot is selected.

Before any fixture action, check required fields and current analysis. A changed
DOM, missing/ambiguous control, unknown validation result, changed artifact or
expired deadline returns a review/failure outcome. File attachment and successful
native validation are distinct from submission and confirmation.

## Submission and confirmation

Do not enable real submission while adding a review adapter. Public adapters declare
`submission=False`; a successful analysis or dry run must not set an application
to `CONFIRMED`. A future live-submit implementation would require its own explicit
authorization, [submission admission](../services/applications/submission_admission.py),
quality/duplicate/limit gates and provider-specific evidence. Registration alone
does not grant those capabilities.

The existing mock flow persists `SUBMITTING` before its one submit click. It proves
confirmation using the independent fixture receipt. A missing or unverifiable
receipt becomes `SUBMISSION_UNCONFIRMED`; cancellation, a worker restart or a
successful HTTP response is not proof of failure or permission to click again.
[Confirmation recovery](../services/applications/confirmation_recovery.py) performs
read-only MOCK receipt reconciliation and preserves uncertainty when evidence is
absent. Controlled dry-run verification stays a dry-run result, never a mock or
employer submission receipt.

## Tests and delivery checklist

Start with deterministic routes, supplied markup and synthetic documents. Add
the provider to the registry-wide contract fixture only when its normalizer and
review boundary are implemented. Keep a network/browser sentinel around pure
analysis so accidental side effects fail visibly.

```sh
python -m pytest -q tests/unit/test_application_adapter_contract.py tests/unit/test_extension_point_contracts.py tests/unit/test_application_form_review_providers.py
python -m pytest -q tests/unit/test_greenhouse_application_adapter.py tests/unit/test_lever_application_adapter.py tests/unit/test_application_upload_integrity.py
python -m pytest -q tests/integration/test_application_form_review_flow.py tests/integration/test_controlled_application_dry_run.py tests/integration/test_application_confirmation_recovery.py
```

When DOM, navigation, validation or file-selection behavior changes, also run the
affected real Chromium tests against the local fixture:

```sh
python -m pytest -q tests/browser/test_greenhouse_application_adapter.py tests/browser/test_lever_application_adapter.py tests/browser/test_controlled_application_dry_run.py
```

Cover supported and rejected route identities; unsafe URLs and confusable fields;
all declared capability flags; privacy-safe error/report projections; changed
forms and reviewed input versions; wrong or modified upload bytes; bounded
cancellation; and restart/receipt behavior where relevant. These tests must not
contact a real ATS or submit an employer application. Record unavailable browser
or connected checks as NOT_RUN, preserving the distinction between static analysis,
fixture execution and actual provider support.

Update the [application area contract](../services/applications/AREA.md), this guide
and user-visible capability descriptions when the supported surface changes.
