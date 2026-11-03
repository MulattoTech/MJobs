# Provider-requested discovery cooldowns

Remotive, RemoteOK, We Work Remotely and Greenhouse retain valid
`Retry-After` instructions on HTTP 429 and server-error responses. A header may
contain ASCII delay-seconds or an HTTP date. Date-based intervals round upward
against the UTC receipt clock, so fractional seconds cannot schedule an early
retry. IMF-fixdate, obsolete RFC 850/asctime forms, and UTC leap seconds are
handled. RFC 850 two-digit years use the fifty-year receipt-clock rule.

The adapters return their existing classified failures with a cooldown; they do
not create a new retry loop. Greenhouse retains its two independent metadata/job
GETs and preserves the longest cooldown from their settled responses. A primary
network error keeps the fixed public `SOURCE_NETWORK_ERROR` classification when
the other response supplies a cooldown. Cancellation/deadline checks still take
precedence.

| Response | Missing header | Malformed bounded header | Minimum valid delay |
| --- | ---: | ---: | ---: |
| Remotive/RemoteOK/WWR 429 | 60 s | 60 s | 1 s |
| Greenhouse 429 | 60 s | 86400 s | 60 s |
| Remotive/RemoteOK/WWR 5xx | 300 s | 300 s | 300 s |
| Greenhouse 5xx | 60 s | 60 s | 60 s |

Signed, fractional, exponent, non-ASCII numeric, duplicate/mixed or trailing-text
values are not valid delay-seconds/HTTP dates. Header text is never included in
diagnostics or durable source metadata. Parsing is limited to 128 characters.
Oversized input selects a fixed interval that triggers the existing disabled
source/max-date backoff, avoiding unbounded integer/date parsing.

Valid large intervals within that input bound are retained rather than shortened.
The existing policy disables automatic collection above 31536000 seconds and
refuses an automatic worker retry. This change does not introduce a new retry cap
or re-enable a disabled source. Operator review is required before resuming it.

## Durable behavior and acceptance scope

Collection persists source backoff before returning the fixed failure. Reopening
the Store retains that deadline. Scheduler admission and the existing read-only
discovery retry plan respect it; an independent due source can still be queued.
Synthetic regressions verify that no failing-source operation is queued just
before the deadline, and that it becomes eligible at the deadline. Extreme
cooldowns remain disabled.

MJ-REQ-160 remains PARTIAL. This slice covers source-discovery GET cooldowns;
browser/AI/queue retry breadth and the complete original requirement remain open.
Application submissions and uncertain non-idempotent writes are not replayed.

Reference: [RFC 9110 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)
and [HTTP date formats](https://www.rfc-editor.org/rfc/rfc9110.html#section-5.6.7).
