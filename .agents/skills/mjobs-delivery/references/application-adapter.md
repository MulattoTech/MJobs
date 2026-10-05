> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Application adapter playbook

Use for application-form preparation, field mapping, submission adapters, or intervention/resume behavior.

## Boundaries
- Draft/preparation is not submission authorization.
- External auto-submit remains OFF unless explicit user/product gates authorize it.
- Never bypass CAPTCHA, MFA, access control, anti-bot protection, or rate limits.
- Never invent required answers, credentials, demographic data, or contact details.
- Ambiguous post-submit state becomes SUBMISSION_UNCONFIRMED; do not blindly retry.

## Required verification
- fixture tests for field mapping, unsupported fields, validation errors, interruption, retry/resume, and duplicate-submission prevention;
- exact artifact/profile/job-version lineage checks;
- browser acceptance through review/preparation;
- explicit tests proving submission gates stay closed when authorization is absent.

## Evidence
Record adapter/provider, supported boundary, review gates, blocked barriers, results, limitations, commit SHA, PR head, and exact-head CI.
