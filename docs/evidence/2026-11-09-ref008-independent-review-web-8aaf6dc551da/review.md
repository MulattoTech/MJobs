# Independent review — PR #991 / MJ-REF-008

## Outcome

No blocking issue was confirmed in the scoped backend review. The six affected repository test modules passed **25 tests and 131 subtests**. A separate probe file outside the repository passed **7 tests and 62 subtests**, exercising additional identity, provenance, versioning, and privacy cases.

The reviewed source was the detached exact head `ca6a66fae084d4254c8044e2edb774b6e5a4a9b5`, tree `3eb631a9a16a731aebbbed0b3b9197427c0140f2`, against base `971ba931418fc271e7a652e27c288e1776448b6d`. The GitHub PR metadata still reported that head when reread at the end of the review. The worktree remained clean. No tracked source, existing tests, acceptance matrices, GitHub comments, commits, or user-PC processes were changed.

This is scoped verification, **not a fresh whole-MJ-REF-008 PASS certification**. Issue #378 is already closed historically; this review does not change that historical state or promote dependent requirements.

- Requirement: https://github.com/MulattoTech/MJobs/issues/378
- PR: https://github.com/MulattoTech/MJobs/pull/991
- Source: https://github.com/MulattoTech/MJobs/blob/ca6a66fae084d4254c8044e2edb774b6e5a4a9b5/services/career_profile/interview_preparation.py
- Working directory: `/workspace/scratch/9cf4231d81ef/mjobs-review991`
- Evidence directory: `/workspace/scratch/9cf4231d81ef/evidence/ref008-review`

## Criterion ledger

“PASS — scoped” below refers to the executed synthetic backend/SQLite checks and reviewed implementation. Browser and deployed-runtime acceptance were not executed.

| Issue #378 criterion | Evidence independently checked | Result and limit |
| --- | --- | --- |
| Structured likely questions, truthful talking points, and STAR prompts grounded in CareerFacts/stories | Existing unit test verifies all three sections, profile version and selected revision. Independent API probe verifies talking-point text equals the approved selected story exactly. Revoking the linked fact approval rejects new use of that revision without rewriting historical packs. Source: service `_build_content` at line 159 and `_eligible_stories` at line 243. | **PASS — scoped.** Verified approved-source grounding and literal copying; no general semantic entailment claim. |
| Source references for claims used in prep | Existing unit test checks source references on every question, talking point and STAR prompt. Independent probe checks the exact selected story-revision reference and linked fact reference and excludes an unselected private fact. A same-name company identity change changes the job fingerprint and question identities while preserving the prior pack. Source: `_story_sources` at line 105; `_build_content` at line 159. | **PASS — scoped.** Pack profile version and immutable story IDs retain provenance context. |
| Missing evidence explicitly marked | Exact-source predicate tests pass 129 subtests. Additional probes cover Unicode marks, punctuation, suffixes, embedded identifiers, ordered phrases, later valid occurrences, and sources that must not be joined. An unselected Java fact does not fill a gap; revoked evidence leaves Rust/Java and interview examples missing. Source: `_matches_topic` at line 124 and `_has_topic_evidence` at line 147. | **PASS — scoped.** Literal identity is tested; negation, aliases, seniority, paraphrases and qualification entailment remain outside this mechanism. |
| Persist and version packs per application/job | Independently altered each of the five stored content fields while keeping the input tuple fixed. Each regenerated pack receives a new ID and the next version, retains every older row unchanged, and reuses the corrected pack on an unchanged repeat. Closing/reopening the Store preserves all versions. Company identity changes version the pack even with an identical display name; irrelevant attributes do not. Repository migration, uniqueness, isolation and downgrade tests also pass. Source: `create_interview_prep_pack` at line 281; list/get at lines 325/344. | **PASS — scoped for profile/job SQLite persistence.** PostgreSQL-specific concurrent pack creation was **NOT_RUN**. |
| Deterministic export without AI availability | Existing unit tests verify export equality on repeat. Independent tests verify clock changes do not create a new corrected pack and synthetic API creation succeeds while real HTTP transport calls are configured to fail. The builder is deterministic local code with no provider invocation. | **PASS — scoped for backend export.** Browser copy/export interaction and deployed provider-outage behavior were **NOT_RUN**. |

## Independent probe design

The new probe file is `test_ref008_independent.py` in this evidence directory. It was not added to the repository or a PR.

| Probe | Additional assertion beyond merely rerunning the author's tests |
| --- | --- |
| `test_new_literal_boundaries_do_not_cross_source_identity` | 18 new boundary/normalization examples, each tested in title, story text and a linked fact; includes later valid Java occurrence after JavaScript, C+++ versus C++, combining marks, VB.NET versus VBXNET and Unicode dashes. |
| `test_phrases_cannot_be_assembled_across_stories_or_linked_facts` | Separate stories and separate linked facts cannot jointly manufacture an ordered phrase; one explicit complete linked fact can support it. |
| `test_unselected_facts_do_not_leak_or_satisfy_missing_evidence` | An approved but unselected private Java fact neither clears the warning nor appears in output. Exact selected source references and copied story text remain correct through the real API router. |
| `test_every_changed_content_field_creates_new_immutable_version` | Independently tests all five equality fields: questions, talking points, STAR prompts, missing evidence and export text. Prior persisted rows remain unchanged, future wall-clock value does not break idempotency, and all versions survive reopening SQLite. |
| `test_revoked_fact_blocks_new_use_but_retains_historical_pack` | Revocation changes new-use eligibility without making the historical immutable pack disappear. Empty selection remains available with explicit gaps and no talking points. |
| `test_company_identity_change_versions_even_when_display_name_does_not` | Distinct canonical companies with the same display name still change source identity/version. An unrelated job attribute change subsequently reuses the current pack. |
| `test_missing_and_foreign_scope_are_sanitized_and_leave_all_state_unchanged` | Missing profile, missing job and foreign story selection produce exact sanitized API codes and leave the full Store snapshot unchanged. A foreign-profile detail request cannot retrieve story text. |

These probes exercise the real service, persistence and ASGI router with fictional data and temporary SQLite databases. The only behavioral patches are a fixed future clock for idempotency and a failing real HTTP transport to make provider calls observable.

## Exact verification commands and results

All commands used the isolated review environment. The root agent's virtual environment was not modified.

### Repository modules — exit 0

```sh
/workspace/scratch/9cf4231d81ef/.venv-review991/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/test_interview_preparation.py \
  tests/integration/test_interview_preparation_api.py \
  tests/integration/test_interview_preparation_scope_and_no_write.py \
  tests/integration/test_interview_preparation_router_mount.py \
  tests/integration/test_interview_preparation_migration.py \
  tests/integration/test_interview_preparation_migration_constraints.py \
  --junitxml=/workspace/scratch/9cf4231d81ef/evidence/ref008-review/affected-exact-source.xml
```

Result: **25 passed, 131 subtests passed, one warning**, in 4.17 seconds. Full stdout/stderr is in `affected-exact-source.log`.

### Independent probes — exit 0

```sh
PYTHONPATH=/workspace/scratch/9cf4231d81ef/mjobs-review991 \
/workspace/scratch/9cf4231d81ef/.venv-review991/bin/python -m pytest -q -p no:cacheprovider \
  /workspace/scratch/9cf4231d81ef/evidence/ref008-review/test_ref008_independent.py \
  --junitxml=/workspace/scratch/9cf4231d81ef/evidence/ref008-review/independent.xml
```

Result: **7 passed, 62 subtests passed, one warning**. Full stdout/stderr is in `independent.log`.

### Source and environment checks — exit 0

```sh
git diff --check 971ba931418fc271e7a652e27c288e1776448b6d ca6a66fae084d4254c8044e2edb774b6e5a4a9b5
/workspace/scratch/9cf4231d81ef/.venv-review991/bin/python -m pip check
```

`pip check` reported no broken requirements. Installed distributions with repository version constraints have no pin mismatches. The Python version and complete environment are saved in `environment.txt`.

Two initial collection attempts stopped with exit 2 because the initially minimal environment lacked Redis and then Uvicorn. No tests ran in those attempts. Installing the required repository-pinned dependencies resolved collection; these were environment prerequisites, not product failures. The Uvicorn collection failure is retained separately in `affected-collection-missing-uvicorn.log`. The remaining test warning is the pinned Starlette/FastAPI TestClient warning about future HTTPX2 use.

## Scanner exception

The only added `.gitleaksignore` entry names one exact historical commit/path/rule/line. Its ignored value is:

`bcdb8e13663f81eaad52cfdbefe40ae3a3a3ac7476307219c58e90e72a6af2c3`

I recomputed SHA-256 from Git blob `951ab7d7d48a2982a4508e2a57d1822e3a0249e3:tests/integration/test_career_story_import_api.py`; it exactly matches the value on line 45 of the named historical evidence JSON. This supports the narrow checksum exception. This review did not independently rerun a full secret scan.

## Findings and remaining limits

**Confirmed blocking findings: none in the reviewed scope.** No production fix is proposed by this review.

Not executed by this review:

- Browser interaction or deployed/live runtime acceptance.
- PostgreSQL-specific simultaneous pack creation.
- The full repository suite, web build or frontend Vitest.
- Hosted check reexecution or an independent review of every hosted job.

The PR author's broader validation claims were not counted as this review's own execution. No integration, deployment or acceptance-matrix promotion was performed.

## Evidence identity

| Artifact | SHA-256 |
| --- | --- |
| Exact service source | `22ed9c823a585b384fba410dff619e950a202246133d6396a3ded74aeace7740` |
| Independent probe file | `7d0b1c98774e3f3d236d9f37d2807f737217154aaa685f15fee8c83f945a7ba2` |
| Machine-readable receipt | `9abbbe6c76a58b80bc044e0a828862a8fcf4de27e862ee9d7194feed69e75a9f` |

`receipt.json` records exact argument arrays, exit codes, source identity, environment limitations and evidence hashes. `source-hashes.json` records SHA-256 and Git blob IDs for the examined service/router/requirements/test files. `github-context.json` records the issue criteria and freshly read PR head metadata.

