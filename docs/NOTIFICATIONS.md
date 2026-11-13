# Optional ntfy activity notifications

Requirement: **MJ-REQ-204.N02** ([issue 363](https://github.com/MulattoTech/MJobs/issues/363)),
under [MJ-REQ-204](https://github.com/MulattoTech/MJobs/issues/205).

MJobs can send privacy-minimal activity notices to an operator-selected ntfy topic.
This separate worker is **off by default**. Existing in-app notifications and
`MJOBS_NOTIFICATIONS_ENABLED` keep their existing behavior.

## Supported behavior

The server-side provider sends generic text for the latest observed terminal state
of these explicitly profile-owned operations:

| Ownership | Supported operations |
| --- | --- |
| Profile ID is the operation entity | Discovery, document preparation, AI evaluation |
| Owned application ID and matching profile parameter | Application dry-run, application form review, uploaded-resume comparison |
| Owned MOCK application; validated v1 snapshot/profile/fingerprint or exact legacy submit-only request | Fixture application |
| Owned uploaded-resume ID and matching profile parameter | Resume extraction |

States: COMPLETED, FAILED, DEAD_LETTERED, BLOCKED and CANCELLED. Unknown kinds,
unproven ownership, source-wide operations and other profiles are excluded.
For example: “Application dry-run completed. Open MJobs for details.”
A completed operation **does not claim that a real application was submitted**.
No profile name, resume text, employer, job title, document, operation error,
raw parameters or application answers are sent.

Capabilities describe the execution surface as `server`. This provider does not
implement browser desktop notifications, mobile background services, attachments,
email, application submission or a full immutable event stream. The parent lists
other potential events; excellent-match, confirmed-submission, authentication-expiry
and employer-discovery notifications are not implemented by this slice.

The API acknowledgement confirms that the ntfy server accepted the generic
message. It does **not** prove a phone/browser displayed it.

## Private configuration

Create a private JSON file outside the repository and outside public/shared
document folders. On Windows, restrict it and its directory to your account.
Use an existing exact MJobs profile ID; there is no cross-profile broadcast.

```json
{
  "version": 1,
  "enabled": true,
  "provider": "ntfy",
  "profile_id": "your-existing-profile-id",
  "server_url": "https://your-ntfy-server.example",
  "topic": "your-private-topic",
  "token_env": "MJOBS_NTFY_TOKEN",
  "state_path": "/data/notifications/deliveries.sqlite3",
  "poll_seconds": 10,
  "batch_size": 50,
  "request_timeout": 10
}
```

For a native Windows runner, use an absolute Windows state path, for example
`C:\\Users\\YourUser\\AppData\\Local\\MJobs\\Notifications\\deliveries.sqlite3`
(with JSON backslashes escaped). For Compose use the container path above.

Use either `token_env` or an absolute `token_file`, never both. Omitting both
supports an operator-controlled topic that requires no authentication. Tokens are
read only when sending and are never written to the delivery journal or output.
A private topic name is not equivalent to authenticated access: configure ntfy
access controls for your destination. There is no default public ntfy topic.

The server URL must be an HTTPS origin with optional port, without credentials,
query, fragment or path. Certificate validation is mandatory. Environment proxies
and redirects are disabled. For an owned local test server only,
`"allow_loopback_http": true` permits a literal loopback address such as
`http://127.0.0.1:18780`; this does not permit plain HTTP to a hostname or LAN address.
Configuration is operator-supplied, never a job payload or public API setting.

No configuration file or `"enabled": false` means no database reads, client creation,
state file or network activity. Reload/restart the worker after changing configuration.

## Run, inspect and recover

Use the normal authorized MJobs runtime environment. The CLI reads
`MJOBS_DATABASE_URL`; it does not migrate or modify the application database.

```text
python -m cli.notifications capabilities
python -m cli.notifications --config <private-file> run --once
python -m cli.notifications --config <private-file> run
python -m cli.notifications --config <private-file> status
python -m cli.notifications --config <private-file> retry <event-key>
```

`run --once` scans one bounded page and sends at most one notification.
The continuous worker polls at the configured interval. Its durable journal also
spaces send reservations at least one second apart across instances sharing that
file. Scheduling after reservation can bunch actual socket writes; exact network
request spacing is not guaranteed.
MJobs CLI status reports event hashes, attempts and fixed diagnostic codes; its
output omits topic, server URL, tokens, provider response bodies and operation contents.
Publishing uses ntfy's JSON POST at the server root, so ordinary HTTPX INFO request
logs also omit the topic and token. Separately enabled third-party HTTP tracing
can expose the server origin or message body; do not enable payload tracing for
private destinations.

The first enabled run persists an activation timestamp. Older completed operations
are excluded. Stable-ID sweeps revisit current terminal records without unbounded
queries; repeated observations are deduplicated by operation/kind/state/worker-attempt.
Transitions that begin and end entirely between polls are not reconstructed.

The private SQLite journal atomically reserves each event before sending. It must
remain on persistent storage. Restarting with that journal does not send acknowledged
or unconfirmed events again. Multiple processes using the same journal cannot claim
the same event. Separate journals are separate subscriptions and must not be used
to defeat deduplication.

| Status | Meaning and action |
| --- | --- |
| PENDING | Queued, not sent yet |
| SENDING | A reserved request may be active |
| DELIVERED | Matching ntfy server acknowledgement observed |
| REJECTED | Credential problem, explicit 401/403/429 rejection, or failed connection before HTTP request bytes |
| UNKNOWN | Timeout, uncertain transport, invalid/missing acknowledgement, non-200 uncertain result, or interrupted attempt |
| FAILED | Invalid local event; no send |

A stale SENDING attempt is displayed as UNKNOWN. It is never automatically
replayed. A process can fail after the server accepts a message and before the
journal records the acknowledgement; exact-once delivery cannot be guaranteed.
An operator may inspect the destination privately, but this version provides no
force-retry for unknown outcomes.

Only REJECTED outcomes with the documented safe codes can be explicitly queued
with `retry`. A rate-limit response preserves Retry-After and blocks all new
requests to that subscription during the cooldown. The rejected event itself
still requires explicit retry. Each event permits at most three total attempts.
Fix credentials before retrying. A retry command updates the queue; a subsequent
run performs the request. No failed event is retried in a background loop.

The journal binds the profile, provider and destination. Changing the topic or
server while reusing it fails closed; rotating a credential is permitted.
Do not delete or alter the journal to force another send. Back it up while the
worker is stopped, alongside its private configuration, and restore both together.
The existing application database backup does not include this separate volume.

## Optional Compose service

After the source is integrated and the ordinary guarded release is qualified,
place `config.json` (and optional token file) in a private host directory.
For a token file, use `/run/mjobs-notifications/ntfy.token` inside the configuration.
Set `MJOBS_NOTIFICATION_CONFIG_DIR` to that existing directory. Set
`MJOBS_NTFY_TOKEN` only if using `token_env`.

```text
docker compose -f compose.yml -f compose.notifications.yml --profile notifications up -d notification-worker
docker compose -f compose.yml -f compose.notifications.yml --profile notifications exec notification-worker python -m cli.notifications status
docker compose -f compose.yml -f compose.notifications.yml --profile notifications stop notification-worker
```

The optional overlay uses the existing image, private database connection file
and egress network. It mounts no document or credential database volume.
Its notification journal has a separate persistent `notification-state` volume.
Loading the overlay does not enable external application submission. Existing
production migration/release guards still apply.

## Validation and provider references

Deterministic tests:

```text
python -m pytest -q tests/unit/test_notification_configuration.py tests/unit/test_ntfy_provider.py tests/integration/test_notification_delivery.py
```

A separate opt-in smoke test requires an **owned, unauthenticated local ntfy
server**. Set `MJOBS_ENABLE_NTFY_LIVE=1` and
`MJOBS_NTFY_LIVE_URL=http://127.0.0.1:<owned-port>`, then run
`tests/integration/test_ntfy_live.py`. It refuses non-loopback destinations,
uses a random synthetic topic, publishes one generic synthetic outcome through
the ordinary runner/provider, polls cached messages and verifies restart dedupe.
It never calls a public notification topic or enables production configuration.

Official contract: [publishing](https://docs.ntfy.sh/publish/),
[authentication and access control](https://docs.ntfy.sh/config/),
[message polling](https://docs.ntfy.sh/subscribe/api/).
ntfy sequence IDs update client notifications but append server messages; this
implementation does not treat them as idempotency keys.

Last executed checks and exact source revisions belong in
`docs/evidence/2026-11-13-ntfy-delivery.json`. Code presence, a skipped local smoke,
or a passing fixture is not provider runtime evidence or formal acceptance.
