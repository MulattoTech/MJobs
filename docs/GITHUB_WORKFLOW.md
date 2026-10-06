# GitHub issue and pull-request workflow — local-only

GitHub Actions is disabled at repository level and each known workflow is manually
disabled. Checked-in workflow jobs also have a literal false guard and no push/PR
triggers. Keep them for reference, not execution. No new hosted run/review is allowed.
No repository rulesets or webhooks were found at cutover; account-level external-app
review settings are not exposed by that read and must not be assumed disabled.

## Local acceptance replaces hosted merge gates

The user's 2026-10-05 instruction supersedes earlier requirements for independent AI
review and exact-head push/PR Actions. Do not retry a billing-blocked job. Read old
logs only as historical evidence. Do not invoke reviewer agents anywhere.

Use one local writer, explicit focused commands, and synthetic temporary data. Build
before browser checks. Require actual non-skipped acceptance and negative controls.
Preserve immutable history, profile boundaries and external-submission OFF. Never
relabel unavailable Docker/PostgreSQL tests as SQLite coverage or broad release PASS.

Record exact source SHA/tree or input hashes, base, command, UTC time, platform,
counts, result and limitations under docs/evidence. Raw CLI transcripts are private.
Run existing catalogue/progress/skill checks when their inputs change. Documentation
edits after a product test require unchanged executable-input proof and metadata
checks; changes to executable inputs need new affected local evidence.

## Publication

Commit only intended owned files. Push each coherent checkpoint to the working
branch and verify the remote SHA. WIP may remain on a published draft branch, never
unverified main. Do not force-push, bypass protection, or create fake green checks.

After local acceptance, use a PR only when it will not request external agents.
When that cannot be established, an ordinary non-force fast-forward push to main
is authorized if current main is an ancestor of the exact candidate. Recheck current
remote main immediately before pushing; reject divergence. Verify remote main after
integration and reconcile the primary checkout only with confirmed idle ownership.
Document the commit and any existing PR's integrated state without claiming hosted
or independent-review acceptance. Close no broader requirement by implication.

No new budget authorization is implied. No Actions, automatic review, cloud test,
self-hosted GitHub runner, provider spend, private-data transfer or deployment.

## Reference-derived local pre-merge checklist

For every material reference-derived change, identify its MJobs issue and pinned
source in the PR template or local integration evidence. Use the clean-room default
and prohibit stealth, CAPTCHA, anti-bot, fingerprint, MFA, credential, access-control,
proxy/block, and rate-limit circumvention regardless of the source license. Before
any intentional source or asset incorporation, review the exact file/ref, license
compatibility, attribution, notices, modifications, patent obligations, and
distribution scope. Update `THIRD_PARTY_NOTICES.md` before integration. Run:

```powershell
python -m tools.verification.reference_provenance
```

The registry validator checks the pinned provenance records and notices. It does not
replace candidate-diff review or establish legal approval.

## Issue and queue conventions

`planning/ISSUE_SEED.jsonl` is the machine-readable source for backlog creation.
Every issue has `<!-- mj-requirement:ID -->`; sync by marker, never mutable title.

The catalogue contains one issue for every numbered requirement plus independently shippable integration
children. `planning/GITHUB_ISSUE_MAP.json` records IDs to GitHub issue numbers after synchronization.

`planning/PR_QUEUE.json` is a plan. Statuses:
`QUEUED`, `BLOCKED`, `ACTIVE`, `DRAFT_PR`, `READY`, `MERGED`, `SUPERSEDED`.

Rules:
- no empty PRs;
- max 4 active by default;
- one coherent reason per PR;
- link requirement IDs and GitHub issues;
- include implementation + required tests/migrations/docs;
- report commands actually run and results;
- main stays locally verified; hosted checks remain disabled;
- dependency order beats raw parallelism.

Branch: `codex/mj-pr-###-short-slug`.

PR body minimum:
Purpose; Requirements/Closes; Architecture impact; Data/migration impact; Safety/truthfulness impact;
Tests/results; Evidence; Known limitations; Rollback/recovery.

Close an issue only when acceptance behavior is verified or explicitly superseded/not-planned.
A merged PR alone is not PASS evidence.

## Parallel lane publication and integration

For concurrent Codex, Antigravity and local-Ollama development, follow
`planning/lanes/README.md`. Feature branches may publish their own code/tests and
unique evidence without holding the integration lock, but must not update canonical
planning or root `main` concurrently.

Before implementation, create/update a machine-local claim with
`python -m tools.coordination.lane_coordination claim ...` and mirror the sanitized
claim on the owning GitHub issue. Before integrating, acquire the atomic local lock.
Only the lock holder may reconcile canonical planning and fast-forward `main`.
Release it immediately after remote/local main and canonical state agree.

Never use `git add -A` in a parallel feature lane and never use plain `git push -f`.
If an owned feature branch truly requires rewritten history, use force-with-lease only
after verifying remote ownership and expected SHA. Never rewrite main.
