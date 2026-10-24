# Troubleshooting MJobs

MJ-REQ-287 / [issue #288](https://github.com/MulattoTech/MJobs/issues/288).
Use this guide to diagnose an existing local workspace, not to certify that a
provider, application, or deployment is working. A successful health check is
not an application receipt. External job submission stays **OFF** throughout
these procedures. A local mock receipt is not an employer receipt.

## Before changing anything

1. Record the operation ID, selected profile, operation state, error code,
   last progress time, and UTC observation time from **Activity**. Record the
   checkout revision with `git rev-parse HEAD`; do not reset or clean the checkout.
2. Identify the actual Compose project and UI port used to launch this workspace.
   The normal fixture defaults are `mjobs` / `18700`; personal defaults are
   `mjobs-personal` / `18701`. Custom launch parameters override these defaults.
   Never switch modes or point a second workspace at the same database to retry.
3. Diagnose once, correct one identified cause, then make at most one deliberate
   retry when the UI permits it. If the same failure returns, stop and escalate.
   Existing scheduled retries must respect cooldown/backoff; do not add a loop.
4. Preserve databases, document revisions, browser profiles, receipts, and audit
   history. Do not delete volumes, flush Redis, edit operation rows, clear leases,
   change permissions globally, terminate unrelated processes, or reboot the host.

### Read-only local checks (PowerShell, repository root)

First list projects; then replace the example values with the **confirmed existing**
workspace. These commands inspect state; they do not start or restart services.

```powershell
docker compose ls
$project = 'mjobs-personal' # Example only: select the existing workspace explicitly.
$port = 18701              # Use its actual configured port.
docker compose -p $project -f .\compose.yml ps --all
curl.exe --max-time 10 --silent --show-error "http://127.0.0.1:$port/ready"
docker compose -p $project -f .\compose.yml logs --since 10m --tail 80 api
```

`/ready` (also `/readiness`) returns HTTP 200 for ready or 503 for degraded,
with `checks` for configuration, database, queue, workers and scheduler; PostgreSQL
adds schema and fixture mode adds mock readiness. Inspect the JSON, not merely
curl's exit code. A connection failure differs from a responding degraded API.
`workers` reports individual queue readiness; a running container alone is not
proof of a fresh worker heartbeat. Allow the configured startup grace period
before concluding that a just-started worker has stalled.

Use the same bounded log command with only the affected service below. Logs are
**private diagnostic material**: inspect locally and redact before sharing.
Do not run `docker compose config`, dump environment variables, or print connection
strings to collect a support report; resolved configuration can expose secrets.

Sources: [launcher](../Start-MJobs.ps1), [Compose service definitions](../compose.yml),
[readiness endpoint](../apps/api/main.py), [runtime health](../services/runtime_health.py),
[local user testing](USER_TESTING.md), [workspace mode recovery](runbooks/WORKSPACE_MODE.md).

## 1. Source broke

**Symptoms:** collection fails, results stop updating, or Sources shows failed
health, `SOURCE_CHANGED`, `SOURCE_NO_VALID_JOBS`, `SOURCE_COOLDOWN`, or
`SOURCE_BACKOFF`. An empty collection is not proof that all employer jobs closed.

**Diagnose:** inspect the source's enabled state, provider/board identity, last
attempt, error, and next allowed attempt. Inspect `source-worker` logs with the
bounded command above. Distinguish a malformed response or changed schema from
rate limiting, network failure, and an intentionally disabled source.

**Recover:** correct an explicitly verified source configuration through the UI,
or wait until the recorded cooldown/backoff expires before one **Collect now**.
Retain prior jobs and raw evidence; do not change native posting IDs to force import.

**Stop/escalate:** repeated schema errors need an adapter fix with a sanitized
response shape. A block, authentication challenge, or rate limit is not permission
to rotate proxies, bypass access controls, or poll faster.

Sources: [source collection and health](../services/discovery/sources/collect.py),
[bounded worker retries](../workers/retries.py).

## 2. Company discovery failed

**Symptoms:** an employer is not enrolled, discovery requests review, or reports
`COMPANY_OFFICIAL_WEBSITE_REQUIRED`, `COMPANY_CAREERS_LINK_UNPROVEN`,
`COMPANY_ATS_REVIEW_REQUIRED`, or `COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED`.

**Diagnose:** inspect the company/source evidence and `company-discovery-worker`
logs. Check whether the employer identity, official domain, career link, and ATS
tenant are actually established; a same-name company or aggregator link is not
equivalent evidence. Keep conflicting candidates visible for review.

**Recover:** supply/correct only a verified official website using supported
company controls, then request one new discovery after the underlying cause is
resolved. Review a proposed source migration instead of replacing existing source
identity or deleting its history.

**Stop/escalate:** unresolved identity, conflicting domains/tenants, unproven career
links, or blocked redirects require human review. Do not manually mark discovery
successful or invent a tenant to unblock enrollment.

Source: [company discovery decisions and review events](../services/company_discovery/flywheel.py).

## 3. ATS changed

**Symptoms:** a formerly recognized route is unknown/ambiguous, form review is
required, or new required fields stop preparation. A detected provider does not
mean that automatic application is supported.

**Diagnose:** compare the current public route/provider evidence with the stored
route and latest form review. Inspect the application's preflight and attempt
history; distinguish stale review from a genuinely different form. Do not paste
filled forms or session-bearing URLs into a report.

**Recover:** request a fresh supported form review after verifying the route.
Review unknown fields explicitly. Keep the application paused until required
answers and document selection are reviewed against current inputs.

**Stop/escalate:** conflicting ATS evidence, unsupported adapters, changed route
identity, or unknown required fields need a reviewed adapter/route change. Never
patch selectors locally to skip required fields or reuse a stale approval.

Sources: [ATS detector](../services/ats_detection/detector.py),
[preflight](../services/applications/preflight.py),
[adapter contracts](../services/applications/adapters/).

## 4. Browser will not launch

**Symptoms:** an operation fails before navigation, reports a browser failure,
or cannot acquire/release browser capacity. No launch does not imply submission.

**Diagnose:** inspect the affected operation's error and `worker` logs. Check that
the worker uses the intended built runtime and that browser installation/launch
errors differ from an already-owned browser session. Do not open private browser
profiles with a different browser version or copy them into another workspace.

**Recover:** for a missing runtime dependency, follow the supported
[installation procedure](INSTALLATION.md) in the existing workspace after
preserving state; do not globally upgrade packages or disable browser sandboxing.
If the product offers owner-scoped browser recovery, use it only after its checks
confirm the owned session is closed. Retry once only when the operation permits it.

**Stop/escalate:** unconfirmed browser release, ambiguous process ownership, or
repeated launch failure needs investigation. Never kill every Chromium process,
clear capacity counters, or remove profile locks to force progress.

Sources: [mock browser execution](../services/applications/mock_adapter.py),
[controlled dry-run execution](../services/applications/controlled_dry_run.py),
[owner-scoped recovery](../services/applications/manual_auth_recovery.py).

## 5. Login expired

**Symptoms:** `AUTH_REQUIRED`, a login page instead of the expected form, or a
stale user-authentication declaration.

**Diagnose:** verify the intended employer origin and application/profile context
before entering credentials. Inspect the operation state without exporting cookies,
passwords, tokens, or browser storage. A user declaration is not proof that a
usable authenticated browser session exists.

**Recover:** use the supported manual authentication flow, if offered, and complete
login/MFA yourself on the genuine site. Return to the application for a fresh
review; resume only through the supported explicit action. Keep real submission OFF.

**Stop/escalate:** unexpected origin/redirect, stale scope, account lockout, or no
supported auth flow means stop. Do not script passwords, harvest verification
codes, copy someone else's session, or treat successful login as submission consent.

Sources: [manual authentication](../services/applications/manual_auth.py),
[authentication recovery boundaries](../services/applications/manual_auth_recovery.py).

## 6. CAPTCHA

**Symptoms:** a challenge page, CAPTCHA indicator, or human-review block interrupts
collection or form work.

**Diagnose:** record only the stable error/state and the provider origin (without
query tokens). Confirm the operation stopped; do not repeatedly reload the challenge.

**Recover:** pause automation. If the provider permits manual access, the user may
handle the challenge in the genuine site's normal UI; this does not authorize an
automated retry or application submission. Resume only a supported, newly reviewed
flow after access is restored and the original operation is reconciled.

**Stop/escalate:** persistent challenges or access denial require provider/user
resolution. **No CAPTCHA or MFA bypass**, solver services, stealth flags, proxy
rotation, session borrowing, or rate-limit evasion. Do not report success while a
challenge is unresolved.

Sources: [application preflight](../services/applications/preflight.py),
[security policy](../SECURITY.md), [application safety constraints](../AGENTS.md).

## 7. Resume extraction failed

**Symptoms:** `NEEDS_OCR`, `UNREADABLE`, an extraction error, missing sections, or
pending facts that differ from the uploaded resume. Stored bytes are not approved
career facts; `READY_FOR_REVIEW` still requires review.

**Diagnose:** confirm the selected profile and supported PDF, DOCX, or UTF-8 TXT
format. Open the original privately to check legibility, corruption, or protection.
Inspect warnings and whether the document contains native text or scanned images.
Local OCR availability depends on the supported runtime; do not send the resume
to a remote OCR service as a diagnostic shortcut.

**Recover:** preserve the original, and if authorized export one readable supported
copy from its trusted authoring tool. Retry extraction once or enter/correct facts
manually, retaining source attribution. Review each fact before verification and
approval; do not silently fill gaps or invent qualifications.

**Stop/escalate:** repeated parser/OCR failure, ownership/checksum mismatch, or
uncertain extraction needs a sanitized synthetic reproduction. Do not weaken
upload limits, bypass document protections, or attach the real resume publicly.

Sources: [extraction contract](../services/career_profile/AREA.md),
[durable extraction validation](../services/career_profile/durable_extraction.py),
[resume/profile retest](runbooks/RESUME_PROFILE_RETEST.md).

## 8. AI unavailable

**Symptoms:** AI work is blocked, times out, fails structured-output validation, or
provider credentials fail a connection check. Saved credentials alone do not enable AI.

**Diagnose:** inspect AI task state, selected provider/model, admission/settings,
timeout/budget information, and `ai-worker` logs. Distinguish disabled AI, invalid
credentials, provider availability/rate limits, and rejected output. Never include
prompts, applicant facts, API keys, or provider response bodies in public diagnostics.

**Recover:** use non-AI/manual workflows while unavailable. Correct only the identified
configuration in supported settings. A **Test connection** action contacts an
external provider and must be explicitly chosen by the user; it is not an offline
check. After access is restored, retry one admitted task within existing budgets.

**Stop/escalate:** do not change billing, increase budgets, switch to a paid model,
or send profile data to an alternate endpoint without explicit authorization.
Do not replace the master encryption key to fix provider authentication: existing
credentials depend on the original key. Preserve rejected output as unapproved.

Sources: [provider credentials and checks](runbooks/PROVIDER_CREDENTIALS.md),
[AI task consumer](../services/ai/consumer.py).

## 9. Database unavailable

**Symptoms:** API readiness returns degraded with database/configuration/schema
false, or operations cannot read/write durable state.

**Diagnose:** inspect `postgres`, `migrate`, and `api` service state and bounded logs.
Configuration or schema failure is not necessarily a stopped database. Verify the
project and workspace mode before considering recovery; never expose database URLs.

**Recover:** if the known existing project is stopped, use its documented startup
procedure with the original project/mode settings. If migration or restore is
needed, stop here and follow the guarded [backup and migration runbook](runbooks/LOCAL_BACKUP.md)
under explicit operator control. Preserve the original database and encryption key.

**Stop/escalate:** corruption, schema mismatch, missing key, or ambiguous project
identity needs a reviewed recovery plan. Do not reinitialize PostgreSQL, delete
volumes, apply ad-hoc SQL, or run migrations while writers are active.

Sources: [readiness checks](../apps/api/main.py),
[workspace configuration recovery](runbooks/WORKSPACE_MODE.md).

## 10. Queue unavailable

**Symptoms:** readiness reports queue false; accepted operations remain queued or
dispatch cannot reach Redis. A queued row is not proof that a worker executed it.

**Diagnose:** inspect `redis` state and bounded `redis`, `api`, and affected worker
logs. Compare the durable Activity state and operation ID across a reload; do not
submit a new operation just because the previous response was lost.

**Recover:** restore the existing project's Redis service using the supported
startup procedure. Let the durable outbox dispatch pending work, then observe the
same operation. Use an explicit retry only if it reaches a retryable terminal state
and the original cause is resolved.

**Stop/escalate:** repeated dispatch failures or mismatched queue/workspace settings
need investigation. Never flush Redis, clear queues, clone task IDs, or reconstruct
application work manually; the database and audit history remain authoritative.

Sources: [outbox dispatch](../workers/dispatch.py),
[dead-letter operation controls](runbooks/DEAD_LETTER_OPERATIONS.md).

## 11. Worker stalled

**Symptoms:** no progress for an operation, stale/missing worker readiness, an
expired lease, or an operation that moves to `DEAD_LETTERED` after bounded attempts.

**Diagnose:** compare the last progress/heartbeat time with the task's configured
deadline and current state. Inspect the relevant service (`source-worker`,
`company-discovery-worker`, `document-worker`, `ai-worker`, or `worker`) and scheduler
readiness. A long but progressing task differs from a worker that has lost its lease.

**Recover:** use **Cancel** only when the product allows it, then wait for confirmed
terminal state. For safe queue-backed dead-letter work, inspect the recorded cause
before one **Retry task**. Recovery must retain the same durable operation/history;
follow the [dead-letter runbook](runbooks/DEAD_LETTER_OPERATIONS.md).

**Stop/escalate:** `SUBMITTING`, `SUBMISSION_UNCONFIRMED`, a reserved attempt, or an
uncertain receipt requires reconciliation, not replay. Do not force cancellation,
clear leases, kill unrelated processes, or restart a worker blindly during a
possible submission. `CONFIRMED` must never be repeated as a recovery action.

Sources: [worker lease/state fencing](../workers/tasks.py),
[runtime health](../services/runtime_health.py),
[confirmation recovery](../services/applications/confirmation_recovery.py).

## Escalation packet and verification boundary

Share only: checkout revision, operating/runtime versions, confirmed workspace mode,
UTC observation time, sanitized operation identifier, stable error code, state and
phase, relevant readiness booleans, the single recovery attempted, and its result.
Use synthetic inputs to reproduce a failure. Redact names, email/phone/address,
resume text, answers, credentials, cookies, browser profiles, private paths,
connection strings, and token-bearing URLs. Screenshots/HTML can contain hidden
form values; do not publish raw captures or log bundles.

Escalate to the maintainer with the affected section and source reference above.
State `NOT_RUN` for diagnostics not executed and `BLOCKED` for unresolved access or
environment problems. A documented procedure, healthy process, generated file, or
passing test does not prove provider success, durable confirmation, or release readiness.
