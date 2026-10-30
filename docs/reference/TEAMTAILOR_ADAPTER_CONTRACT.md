# Maintainer reference: Teamtailor review-first application adapter

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/services/applications/adapters/teamtailor.py).


- **Source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
- **Source path:** `services/applications/adapters/teamtailor.py`
- **Full blob SHA:** `53c6727112899219a82993378770f0761a98bc3b`
- **Status:** Static maintainer reference. No source was changed and no tests, browser, network request, upload, or submission was run.

## Scope and safety boundary

`TeamtailorApplicationAdapter` is review-first. It normalizes canonical public Teamtailor job URLs and inspects metadata from an **already-loaded** page. The source explicitly says it never fills controls, uploads documents, or submits applications, and that unsupported or challenge-protected forms require manual review (`services/applications/adapters/teamtailor.py:1-6`). It is registered as the built-in provider `teamtailor` (`services/applications/provider_registry.py:22-34`).

`capabilities()` makes the boundary machine-readable:

- supported: route normalization, form-metadata extraction, and human review for uncertainty;
- unsupported: mock-fixture upload, document upload, live form interaction, and submission (`services/applications/adapters/teamtailor.py:121-133`).

## Entry points

### `TeamtailorApplicationAdapter.normalize_route(route) -> dict | None`

**Input.** `route` must implement `collections.abc.Mapping` and contain:

- `provider`: ASCII string equal to `teamtailor`, case-insensitively;
- `url`: nonempty string no longer than 2048 characters (`services/applications/adapters/teamtailor.py:135-146`).

Optional `tenant` and `requisition` values, when supplied, must be ASCII strings matching the URL-derived values case-insensitively (`services/applications/adapters/teamtailor.py:167-180`).

**Accepted URL.** Only `https://{tenant}.teamtailor.com/jobs/{slug}` is accepted, where:

- tenant is 1-63 ASCII alphanumeric/hyphen characters, begins alphanumeric;
- slug is 1-120 ASCII alphanumeric/hyphen characters, begins alphanumeric;
- scheme is HTTPS;
- parsed port is None, username/password are absent, and fragment is empty (numeric ports and nonempty fragments are rejected);
- the path has exactly `/jobs/{slug}`;
- the host and path are ASCII;
- the original URL contains no character with code point <= 0x20 or equal to 0x7f; this is not a blanket Unicode-control rejection (`services/applications/adapters/teamtailor.py:17-20,135-165`).

A query string is tolerated but discarded. The parsed checks do not separately reject an empty port delimiter or empty fragment delimiter. The normalized result lowercases provider, tenant, and requisition and rebuilds a canonical query-free URL (`services/applications/adapters/teamtailor.py:167-186`).

**Failure behavior.** Unsupported or malformed values return `None`; the method does not emit an error mapping (`services/applications/adapters/teamtailor.py:137-180`). `urlsplit`/port parsing `ValueError` is caught and becomes `None` (`services/applications/adapters/teamtailor.py:147-153`). The focused tests include wrong providers, HTTP, lookalike hosts, credentials, explicit ports, extra/trailing path segments, percent-encoded slash content, fragments, missing tenant subdomain, and conflicting metadata (`tests/unit/test_teamtailor_application_adapter.py:23-60`).

### `TeamtailorApplicationAdapter.analyze_page(page, *, check=None) -> dict`

**Input.** `page` is expected to provide Playwright-like `locator()` objects with `count()` and, for `form input, form select, form textarea`, `evaluate_all()`. The page must already be loaded; this adapter performs no fetch (`services/applications/adapters/teamtailor.py:188-220`). Optional `check` is called at boundaries before and during analysis. If it has callable `remaining_seconds`, the remaining time is converted to milliseconds and clamped to 1-10,000 for control-state evaluation (`services/applications/adapters/teamtailor.py:190-195,210-222`). The shared form analyzer uses the same callback convention and expects exactly one form (`services/applications/form_analysis.py:254-278`).

**Challenge detection.** Before shared analysis, the adapter checks selectors for CAPTCHA, Turnstile, Cloudflare challenge data, site keys, and challenge iframes (`services/applications/adapters/teamtailor.py:21-25,192-201`). A detected marker returns:

```python
{"status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED", "fields": []}
```

If selector inspection fails, it returns the same value-free shape with reason `CHALLENGE_DETECTION_UNAVAILABLE` after rechecking cancellation/deadline state (`services/applications/adapters/teamtailor.py:192-201`). It does not return exception text or markup.

**Shared metadata analysis.** `analyze_form_page` extracts metadata from one form without interacting with controls. It returns `UNKNOWN` for no/multiple forms or DOM-analysis failure, and uses `analyze_controls` for bounded, value-free field metadata (`services/applications/form_analysis.py:123-213,254-278`). If that result is not a mapping or its status is not `PASS`, Teamtailor returns it unchanged (`services/applications/adapters/teamtailor.py:202-205`).

**Visibility/editability enrichment.** A `PASS` result must contain a nonempty `fields` list or Teamtailor returns `UNKNOWN / CONTROL_STATE_UNAVAILABLE` (`services/applications/adapters/teamtailor.py:206-208`). The adapter evaluates a value-free JavaScript projection for every form input/select/textarea. That projection records:

- DOM/ancestor visibility and editability state;
- paint styles (`clip-path`, legacy `clip`, and `contain`);
- control rectangle;
- overflow-clipping ancestor rectangles and axes;
- whether transforms make clip geometry incomparable (`services/applications/adapters/teamtailor.py:26-102`).

The returned control-state list must match field count, each state must be a mapping, and `user_visible`, `user_editable`, and `clip_geometry_transformed` must be exact booleans. Otherwise analysis fails closed as `CONTROL_STATE_UNAVAILABLE` (`services/applications/adapters/teamtailor.py:227-236`). Each field dictionary is then mutated in place to add final `user_visible` and `user_editable` values (`services/applications/adapters/teamtailor.py:234-240`).

If evaluation or timeout calculation raises, the adapter rechecks `check` and otherwise returns value-free `UNKNOWN / CONTROL_STATE_UNAVAILABLE` (`services/applications/adapters/teamtailor.py:210-226`). A cancellation/deadline exception raised by `check` can therefore propagate when the callback is invoked; the adapter does not deliberately convert callback failure into a successful analysis.

### `TeamtailorApplicationAdapter.requires_human_review(analysis) -> bool`

This static gate returns `True` unless all of the following hold:

1. `analysis` is a mapping with status `PASS` and a nonempty field list;
2. every field is a mapping with `ambiguous is False` and `sensitivity == "STANDARD"`;
3. every field has `user_visible is True` and `user_editable is True`;
4. every field is recognized as a narrow standard contact field (`services/applications/adapters/teamtailor.py:242-256`).

The known-field helper allows supported editable input types or selects; it rejects free-form textareas, unknown tags, hidden/file/submit/button/image inputs, and selects with a non-`None` input type (`services/applications/adapters/teamtailor.py:112-115,325-338`). Visible labels, when present, must **all** belong to the known contact identifier set. Without labels, a recognized normalized `name`, `id`, `placeholder`, or `aria_label` is sufficient (`services/applications/adapters/teamtailor.py:103-111,340-359`). No applicant values are consulted.

## Effective visibility and the nested `coordinates()` definition

`_is_effectively_user_visible(state)` is deliberately fail-closed (`services/applications/adapters/teamtailor.py:259-322`). It requires:

- upstream `user_visible is True`;
- nonempty paint-style list, a control-rectangle mapping, overflow-region list, and exact boolean transform flag;
- no transformed geometry when overflow clip regions exist;
- every paint style to report `clip_path == "none"`, `clip == "auto"`, and no `paint`, `strict`, or `content` containment (`services/applications/adapters/teamtailor.py:259-285`).

The nested `coordinates(rect)` helper validates the four rectangle coordinates (`left`, `top`, `right`, `bottom`). It rejects booleans, non-numbers, non-finite values, and non-positive width/height, returning `None`; otherwise it returns a four-number tuple (`services/applications/adapters/teamtailor.py:287-300`). Its caller treats `None` as not visible. For every overflow region, the outer helper also requires exact boolean clipping-axis flags, valid geometry, and full containment on each clipped axis (`services/applications/adapters/teamtailor.py:302-322`).

This helper is local implementation detail: it cannot be imported as a module API.

## Safe synthetic examples

These examples are illustrative and were not executed. They exercise pure metadata methods, not network, browser interaction, upload, or submission.

```python
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter

adapter = TeamtailorApplicationAdapter()
route = adapter.normalize_route({
    "provider": "TEAMTAILOR",
    "url": "https://Acme.teamtailor.com/jobs/senior-engineer?source=fixture",
})
# Source-proven result shape:
# {
#   "provider": "teamtailor",
#   "tenant": "acme",
#   "requisition": "senior-engineer",
#   "url": "https://acme.teamtailor.com/jobs/senior-engineer",
# }
```

The same normalization shape is asserted by the focused test (`tests/unit/test_teamtailor_application_adapter.py:23-33`).

A synthetic review-gate example:

```python
analysis = {
    "status": "PASS",
    "fields": [{
        "ambiguous": False,
        "sensitivity": "STANDARD",
        "tag": "input",
        "input_type": "email",
        "name": "email",
        "labels": ["Email"],
        "user_visible": True,
        "user_editable": True,
    }],
}
needs_person = adapter.requires_human_review(analysis)  # False by source contract
```

Any ambiguous, sensitive, custom, hidden, disabled, readonly, unsupported, or incompletely evidenced field makes the result `True` (`tests/unit/test_teamtailor_application_adapter.py:96-169,206-247`). This method classifies metadata only; it does not authorize form filling or submission.

## Nearby test evidence (read, not run)

At the pinned commit, the focused unit source asserts:

- the exact capability declaration and unsupported actions (`tests/unit/test_teamtailor_application_adapter.py:12-21`);
- canonical route reconstruction and adversarial route rejection (`tests/unit/test_teamtailor_application_adapter.py:23-60`);
- reuse of the shared value-free analyzer and challenge fail-closed output (`tests/unit/test_teamtailor_application_adapter.py:62-94`);
- manual review for unknown, empty, ambiguous, sensitive, custom, hidden, noninteractive, or missing-positive-state fields (`tests/unit/test_teamtailor_application_adapter.py:96-169`);
- attachment of value-free visibility/editability evidence (`tests/unit/test_teamtailor_application_adapter.py:171-204`);
- review for CSS-hidden, disabled, readonly, and unsafe select controls (`tests/unit/test_teamtailor_application_adapter.py:206-247`);
- fail-closed handling of clip paths, legacy clipping, paint containment, overflow clipping, and transformed clip geometry (`tests/unit/test_teamtailor_application_adapter.py:249-376`).

Those tests were inspected, not executed. Live/public Teamtailor behavior, browser compatibility, network behavior, uploads, and submission are **NOT_RUN** for this maintenance task.
