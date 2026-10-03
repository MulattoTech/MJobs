# Public company discovery prerequisite

PR474 merged at216f1a4696abc8dcb07fb57b5a67102091343ec5. Live Git/GitHub overrides prior planning publication snapshots. Root and historical worktrees remain preserved.

The remaining MJ-REQ-083 link-accuracy workflow requires a production-safe fetch path. Company discovery currently blocks all PERSONAL operations and has only a fixture client. This slice makes the existing user-added company workflow usable with real HTTPS while preserving uncertainty. It advances original MJ-REQ-054/060/070..073 as PARTIAL; full automatic enrollment, provider breadth, scheduling and application-link accuracy remain required follow-ups.

- Add exact-origin HTTPS transport with numeric DNS pinning, TLS hostname checks, bounded resolver work, request/body/redirect/deadline limits, robots policy and cancellation. Never enable proxy/cookie/auth, arbitrary cross-origin requests, fixture bypass or external submission.
- Explicitly mark newly queued PERSONAL operations PUBLIC_HTTPS. Historical fixture operations and fixture-mode/public-mode mismatches remain blocked.
- Bind an existing company only with qualifying stored verified domain evidence. Claimed names/ATS tenant strings cannot bind an arbitrary site. New sites use stable origin-based candidate identity and stay unverified.
- Generic RSS/JSON feed discovery does not create a fictional ATS tenant. New unknown company sources start disabled/review-required; explicit user enablement remains an independent preference and is preserved on rediscovery; explicit one-time user-requested collection may display their unverified job evidence. They do not suppress later discovery as already monitored.
- Add the PERSONAL Companies/Sources form, durable operation status and blocked-state feedback. Preserve private feedback and existing fixture workflows.

Validate observed RED, focused policy/identity/worker tests, actual browser->API->worker->persisted readback, full regression, frontend/build, catalogue/progress, independent boundary review and bounded live public HTTPS reads. Synthetic HTTP responses are not live network proof; record them separately. Publish/merge only after exact-head hosted CI and review gates.
