# Remotive malformed posting ID isolation

- Run: `web-20261105T012939Z-3b304a06cfc3`.
- Requirements: MJ-REQ-017.S08 / parent MJ-REQ-017; issue #309.
- Baseline: `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1`.
- Tested source: `ab494ec700b254666a1bfa2773fd5bad9b5d5369`.
- Ownership: `refs/heads/webpro-claims/MJ-REQ-017.S08`, owner
  `7ba361eed4a8fc8474badfb8a8e68c69d7f5fb67`; HTTP 201 creation and exact
  readback at 2026-11-05T01:32Z. Consult the live ref for current ownership.

## Defect and correction

Remotive permits integer and string posting IDs. Its numeric conversion used
`isdigit()`, which also accepts characters such as superscript and circled
digits that `int()` cannot parse. Python's configured integer conversion limit
also rejects excessively long numeric strings and integer-to-string conversion.

Those failures escaped as plain ValueError. The shared collector intentionally
handles SourceError per posting, so one such provider record instead aborted the
whole batch, losing the valid jobs from that attempt.

The adapter now converts these ID failures into the fixed
`SOURCE_INVALID_JOB` classification with suppressed underlying exception display.
The existing collector rejects that posting, retains valid jobs and reports
PARTIAL source health. Supported integer/string identities, decimal Unicode,
leading-zero normalization and raw provider evidence remain unchanged.

Only the adapter and dedicated regression files change product/test behavior.
The shared collector, provider transport, schema, cooldown policy, UI, external
application permission, and canonical acceptance authorities are unchanged.

## Executed verification

The test files were authored before the production correction. At the baseline:

- Unit regression: 5 failed, 14 passed. Three unsupported-digit strings and two
  integer conversion limits escaped as ValueError.
- Pinned-environment unit plus collection regression: 8 failed, 14 passed.
  The three real collector cases each aborted at Remotive.normalize before
  persisting the otherwise valid posting.
- The first global-Python combined attempt did not collect integration tests:
  installed pydantic-core 2.50.3 did not match that global Pydantic's required
  2.50.2. No global package was changed. Tests proceeded in this run's isolated
  venv using repository-pinned application/test dependencies.
- The initial corrected-source run had 65 passes and 3 test-fixture failures:
  repeat collection correctly encountered SOURCE_COOLDOWN. The fixture now
  explicitly asserts that immediate retries are rejected without a request,
  then ages last_attempt only in its disposable database. Its final health
  assertion follows the existing PARTIAL state for a rejected posting.

Final focused command, with the same executable/test bytes committed as the
tested source above:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/unit/test_remotive_job_ids.py tests/integration/test_remotive_job_id_isolation.py tests/unit/test_remotive_url_contract.py tests/unit/test_remotive_body_deadline.py tests/integration/test_remotive.py --tb=short --junitxml=.run-3b304a06/focused-final.xml
```

**68 passed, zero failures, errors or skips, in 21.03 seconds.**

The 22 new cases cover invalid type/empty IDs, unsupported numeric characters,
conversion limits and the valid boundary, preservation of supported identities,
fixed error diagnostics, and three synthetic mixed-feed collection journeys.
Each collection journey performs two successful collections separated by an
explicit cooldown refusal, reopens SQLite after each successful collection,
checks single-job/posting/snapshot idempotency and exact raw ID provenance,
verifies PARTIAL health with zero failures/no error, reads the valid job through
the real API TestClient, and confirms manual-only capability and zero applications.

Existing Remotive URL boundary, timeout/cancellation, provider-error, payload
limit, registry and collection tests also pass. All provider responses are
synthetic MockTransport data; there is no live provider traffic or paid model use.

Additional checks:

```text
python -m tools.verification.catalogue                PASS
python -m tools.verification.progress validate        PASS
python -m tools.verification.progress_dashboard --check CURRENT
git diff --check                                     PASS
python -m tools.verification.commit_provenance --commit HEAD
                                                    PASS (3 source files)
```

## Publication and acceptance limits

At this source checkpoint, hosted required checks, independent review,
actual-candidate integration and deployment remain NOT RUN. Self-review of this
patch is not an independent approval. No production rollout was attempted.

MJ-REQ-017.S08 was already canonically PASS at the inspected baseline; this
regression repair adds no acceptance credit. The original MJ-REQ-017 parent
remains PARTIAL. Original/reference/parity denominators and Project/Fleet
acceptance state have not been edited. The issue remains open.

The change has no migration. Rollback is a protected revert of this adapter
change followed by the existing guarded release workflow, preserving data.
The existing integration owner must refresh main, required checks, review
threads and exact candidate before merging. Production remains a separate,
exclusively owned guarded deployment with version and journey verification.
