# MJobs final release audit prompt

Audit MJobs against evidence, not intent. Do not assume merged PRs equal PASS.
Verify fresh startup/migrations, persistence, mock discovery flywheel, canonical/application-route dedupe,
matching, factual document gates, mock ATS prepare/fill/validate/submit/confirm, intervention resume,
idempotency, crash/restart recovery, backup/restore, static/unit/integration/browser/E2E, Docker/Compose,
and bounded live read-only discovery where safe.
Report only PASS / FAIL / PARTIAL / BLOCKED_EXTERNAL / BLOCKED_ENVIRONMENT / NOT_RUN / NOT_APPLICABLE,
with command/action and evidence for every PASS. Never use real job submission as certification.
