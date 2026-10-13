# MJobs — Astra Medium local implementation

- Sole owner thread `01a1360d-b31a-7023-8d19-45fbdcb72e74`, verified `gpt-6-astra`, medium; root `C:\Tools\Dev\MJobs`. Integration lock held; no subagents or background development loop.
- Current branch `feat/astra-profile-ai-credentials-20261013`; tested source `7e2d0752e57178aae86dff4d298eb727b28fadb9`. Local/remote main `365711aa5639461921bb8ed65cf198902c4255b0`.
- Merged: PR616 fresh-match durable priority (MJ-REQ-119 PASS); PR617 stale AI input/review fences; PR618 grounded current skill coverage and unknown-input handling. Exact PR checks passed; latter umbrella requirements remain PARTIAL.
- Current slice MJ-REQ-108/#109: saved profile key reaches durable OpenAI worker at fixed destination, with rotation/deletion/decryption fences, explicit selection and source readback. Synthetic provider transport only; live inference NOT_RUN. Already-started calls cannot be recalled.
- Verification PASS:1564 units/1023 subtests;791 frontend tests/build;120 service/PostgreSQL/Redis tests/5 subtests;5 connected Chromium tests;26 real Linux durable-priority tests; full canonical Compose mock/UI/restart/fresh restore and restored AI-worker credential unlock with zero calls. Evidence `docs/evidence/2026-10-13-profile-ai-credentials.json`. PR publication pending; self-review only.
- Acceptance unchanged: original108/366, reference19/59, matching_ai3/20, original P0/P1 47/142 PASS. No umbrella promotion.
- Old Sol objective, attempts, lessons and historical claims preserved through supported AFK events. Employer-identity stash `0c7ff43a` and reconciliation backups untouched.
- Owned Compose stacks stopped, synthetic volumes retained, original shared image tag restored. Owned isolated PostgreSQL/Redis fixtures remain for this interactive session. External submission OFF.
- Next: publish this verified slice, then inspect checked provider source quotes/uncertainty; dynamic-form and multi-page controlled DRY_RUN remain larger gaps. Four-hour interactive session ends about20:17 UTC; no unattended continuation.
