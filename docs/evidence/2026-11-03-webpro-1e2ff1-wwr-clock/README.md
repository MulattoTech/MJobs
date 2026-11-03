# WWR date fixture query clock

This test-only correction belongs to MJ-REQ-030 and run webpro-20261103T034313Z-1e2ff1. The original requirement's acceptance state and all literal criteria stay unchanged.

The WWR RSS fixture has a literal oldest posting at 2026-10-04 08:00 UTC. Its public-intake test froze only the semantic evaluator at October 18. The API first applied a separate database cutoff using real time, so the third row disappeared once real time passed November 3 08:00 UTC. Unchanged protected main independently failed with two rows rather than three.

The source commit uses the existing _filtered(..., query_at=...) helper, which scopes the same query instant to both API and semantic date bindings. Every existing intake, RFC2822 provenance, date-window, reopen and replay assertion is unchanged. The RSS bytes and production date filtering are unchanged.

verification.json binds the exact baseline, source, blobs, execution and preserved failure. boundary-probe.py.txt is the frozen executed diagnostic: three separate synthetic test cases with an outer API clock immediately before/after the boundary and in 2030. Before the fix their results were PASS/FAIL/FAIL; after the fix all three pass. They are repeated observations of one test, not additional unique acceptance coverage. No OS clock, external HTTP, production data or other worker resource was changed.

The exact source 19ea133e full module passed 9 parent tests plus 8 subtests, with zero failures/errors/skips. Catalogue, strict journal, dashboard, provenance and whitespace guards passed. Public evidence excludes local host/path values and raw environment. Original hosted failure metadata is attributed to the published 920 receipt; this run did not download that artifact.

Actual receiving-candidate execution and every protected hosted gate are separate requirements. This source does not repair or clear 909's distinct unknown monitoring trigger, 920's restore privacy interaction, or any whole requirement. Source ownership remains claim 4ba651b4 until independently verified integration or explicit handoff.
