> **Current workflow policy:** reviewed public GitHub Actions run read-only CI
> and synthetic integration checks. Paid AI/cloud reviewer sessions and real
> applicant submission remain disabled. Count PASS only from executed, matching
> source revisions; missing/failed checks are never assumed PASS.

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
