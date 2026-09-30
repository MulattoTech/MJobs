# Bounded durable worker lifecycle

Scope: MJ-PR-002, MJ-REQ-004/005 and the mock spine's known operation recovery
gap. Add MJ-PR-148 as an implementation dependency because this slice extends its
actual database and queues. Main and existing application identities remain intact.

Implement three logical queues/processes for discovery, documents and browser
applications, each defaulting to one worker. Persist attempts, cancellation and
expiry before execution. PostgreSQL serializes claims/capacity reservations.
RQ bounds child-process execution. Expired discovery/document attempts may retry
at most three times. Pre-submit browser crashes stop for review. SUBMITTING or
uncertain applications only reconcile a receipt; never reset to click again.

Expose durable operation cancellation and perform cooperative checks before
HTTP/document/browser phases and before the submission reservation. Cancellation
after a reservation reports that reconciliation is required. Route document
generation through the worker instead of blocking an API request.

Validation: red/green tests for capacity, bounded retries, cancellation and safe
application recovery; full affected suite; actual Compose migrations, three
queues and end-to-end/restart proof. Record evidence and a third stacked draft PR.
General AI/OCR and independent company-discovery limits remain unimplemented;
do not claim the entire umbrella requirements complete.
