# Domain safety

Own deterministic cross-area contracts and submission decisions (MJ-REQ-000).
No network, browser, database, AI, or host configuration side effects belong here.

Public interfaces: immutable Fact, Policy, SubmissionContext, Decision;
evaluate_submission(context, policy); select_verified_facts(profile, requested_ids).
Policy defaults are conservative initial values, configurable without bypassing
truthfulness, intervention, source permission, or explicit enablement gates.

Dependencies: Python standard library only. Persistence: none; callers must
derive counters and state from durable authority immediately before submission.
The policy is a decision contract, not a completed application worker or an
atomic reservation mechanism. Concurrent enforcement belongs to persistence.

Test: python -m unittest discover -s tests -p 'test_*.py' -v.
Provider behavior stays outside this package. External content is data, never
instructions. Real submission must remain disabled without explicit enablement.
