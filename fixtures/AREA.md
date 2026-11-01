# Fictional source/company/ATS environment

Own local HTTP fixture contract for MJ-REQ-209 through 213. Data describes Alex
Fixture and Example Systems Inc.; it is never user career data or production
discovery output. Interface: create_mock_app and fixture_server. Provider endpoints
are exposed only inside the private Compose network. Receipts have deterministic
application identity and durable mock storage. Tests exercise browser/HTTP/data
behavior; acceptance status is recorded separately in the canonical matrix.

`create_mock_app(receipt_file=None, *, scenario="baseline")` and
`fixture_server(receipt_file=None, *, scenario="baseline")` preserve the original
two A source copies by default. The opt-in `coverage` scenario retains those
exact rows and adds distinct B (hourly salary, on-site) and C (unknown employer,
salary and remote status). C has no invented company or website. Coverage feed
tests inspect that unresolved identity at the HTTP boundary; the existing
discovery pipeline must not manufacture an employer to ingest it. Unknown
scenario names fail before the fixture server opens a socket.

`/company` names Example Systems Inc., links to `/company/careers`, and redirects
through that route to `/ats/example`. The portal links to `/ats/example/jobs`,
which exposes separate A and B jobs and their local fictional application URLs.

The default `/ats/apply/{requisition}` remains the existing multipage flow.
Explicit `?flow=onepage` renders all contact, upload, choice, custom-question and
confirmation controls together and posts to `/ats/onepage/{requisition}`.
Both flows share document validation and durable receipt publication. The
one-page handler validates documents and the approved fictional answer before
publishing a receipt; validation failures publish none. A repeated application
identity preserves its original receipt, while reuse for a different fictional
requisition is rejected. Existing `/ats/review` and `/ats/submit` routes remain
available to the mock application adapter and its receipt-recovery fixtures.

Dynamic variants support country changes in both directions. United States
activates the dependent control; Canada or a cleared selection hides it, clears
its obsolete value and removes the browser requirement. The server enforces an
active dependent field independently. The known controlled answer remains
Oklahoma; the unknown-field variant requires a nonempty value when active.
All names, uploads, answers and receipts in these scenarios are synthetic test
data, and none of these routes submits to an external employer.
