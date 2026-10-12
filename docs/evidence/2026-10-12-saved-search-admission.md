# Saved-search admission — #192 / MJ-REQ-191

Selected saved-search collection retains every captured posting and defers primary-profile scoring until the persisted criteria admit canonical candidates. Ordinary collection and secondary-profile recalculation retain their existing behavior. Canonical attributes and remote type now reach the shared search evaluator. Posting candidate/quality-observation lineage establishes source membership even when another source wins the canonical job fields; legacy rows without lineage retain the existing source/endpoint check.

Negative regressions reproduced criteria bypass and source-winner exclusion before repair. Final local verification on the repair branch:

- Focused worker/collection/scheduler/scoring/search semantics: 42 tests and 40 subtests PASS, zero skips.
- Final affected unit selection (`worker or discover or source or scheduler or search or scoring`): 244 tests and 148 subtests PASS, zero skips; 1134 unrelated tests deselected.
- Production web build PASS. Connected first-run Chromium journey: 1 PASS, zero skips. UI-created remote/full-time search captures three synthetic postings, scores only the eligible remote job, and preserves filters and captured counts in durable history/reload.
- Broad unit checkpoint: 1378 tests and 785 subtests PASS before the final lineage correction. The final affected suite, focused integration and browser results cover that correction.
- Catalogue, progress, dashboard, agent-skills and diff checks PASS.

Exact source digests, test XML identities and current commit are recorded locally in `.git/afk-dev/iteration31-verification.json`; private operational evidence is not published. Local Luna review reproduced a source-lineage defect; the fix and follow-up review found no concrete important findings. This is local review, not hosted certification.

Hosted checks for the final candidate, PostgreSQL/Redis and full browser gate remain pending/NOT_RUN locally. Full saved-search acceptance remains PARTIAL: connected preview/source-isolation acceptance and whole release criteria remain separate obligations. No acceptance authority was promoted. Real external application submission remains OFF. Rollback uses a scoped source revert; no schema or private configuration changes.
