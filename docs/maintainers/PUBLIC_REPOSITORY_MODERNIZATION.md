# Public repository modernization

This is the single current modernization plan, replacing the earlier disabled-CI assumptions. Results and any remaining execution limitations are recorded in [MODERNIZATION_STATUS.md](MODERNIZATION_STATUS.md).

## Six phases

1. **Recovery and public safety:** verified private Git bundles, scanner findings classified, CodeQL alerts retained, obsolete public recovery pointers retired only after private verification. No gratuitous main-history rewrite.
2. **CI:** fast independent gates, real PostgreSQL/Redis integration, reusable connected acceptance, sharded full-browser regression, dependency review and policy/documentation checks.
3. **GitHub practices:** native PR checks and auto-merge, secret/push protection, CodeQL, private vulnerability reporting, Discussions, a labeled Project and repeatable Project synchronization.
4. **Repository hygiene:** one canonical current contract, compatibility pointers for old prompts, recoverable obsolete workspaces, and no deletion of ambiguous work.
5. **Public documentation:** actual fixture screenshots, working installation/development entry points, architecture, user/privacy limits and a curated static documentation site.
6. **Release and continuation:** original-source Apache-2.0 license, changelog/version policy, guarded image/release validation, exact-check feedback and tested AFK-DEV continuation instructions.

## Constraints

Personal data stays local/private. No paid extra agents, self-hosted public runners, unsafe approval bypasses, secret replacement, real external applications or fabricated acceptance. The 366 original and 59 reference requirements remain separate.

A complete modernization does not mean MJobs itself is complete. Existing browser and security defects remain actionable product work. The next Codex sprint should resolve high-impact security and connected P0/P1 gaps, not repeat this infrastructure setup.
