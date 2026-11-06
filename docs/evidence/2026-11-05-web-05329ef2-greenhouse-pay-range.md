# Greenhouse optional pay-range overflow regression

## Scope and ownership

- Run: `web-20261105T012826Z-05329ef2-d6f5-4105-95e4-1f074154cb95`.
- Stable claim: `GREENHOUSE-MALFORMED-PAY-RANGE`, linked to [MJ-REQ-018 / #19](https://github.com/MulattoTech/MJobs/issues/19).
- Atomic create-reference returned HTTP 201 and the exact owner readback at 2026-11-05T01:40:01.581921+00:00.
- Claim owner: `421d704742c1adbf7b969ff2b7cf6038fb0d98df`. This no-op coordination commit is excluded from product history.
- Baseline: `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1`.
- Tested implementation: [source commit fe81dbe0fefc530ffa5cb8c3cb56e3942cb7d2d5](https://github.com/MulattoTech/MJobs/commit/fe81dbe0fefc530ffa5cb8c3cb56e3942cb7d2d5).
- Branch: `web/web-20261105T012826Z-05329ef2-d6f5-4105-95e4-1f074154cb95/greenhouse-pay-range`.
- Fifteen open PR file lists had no overlap with the three affected source/test paths when ownership was acquired.

This fixes one regression in the existing official-API Greenhouse collector. MJ-REQ-018 remains PARTIAL. Original acceptance wording, statuses, denominators and separate TalentHop tracking are unchanged.

## Reproduced failure

The optional `pay_input_ranges` guard called `math.isfinite` before testing the existing accepted cents range. Python converts integers for that finite check, so an otherwise valid JSON posting containing a 401-digit positive or negative integer raised `OverflowError`. Both lower and upper bounds reproduced the defect.

The collection regression used a mocked public-API response with two valid postings. One posting's optional salary had an oversized lower bound. The exception escaped normalization and aborted collection instead of ignoring the invalid optional range.

Before changing product code, the new tests reported:

```text
11 failed, 5 passed, 18 subtests passed in 16.83s
```

The failures comprise ten unit subcases and the complete collection case. This is synthetic fault evidence, not a claim that Greenhouse has served such a payload or that a user-data incident occurred.

## Correction

Check the existing numeric type and range bounds before calling `math.isfinite`. The accepted amount range, rejection of booleans/non-numeric values, ordering between lower and upper bounds, currency handling and cents conversion are unchanged.

Invalid optional ranges are ignored. Other valid ranges and the existing description-based fallback remain usable; missing compensation stays unknown. The fix does not add an exception catch around collection or change source-health classification, HTTP transport, provider policy, database schema or application submission.

## Executed verification

In the isolated worktree and private Python environment, with repository-pinned Pydantic 2.13.5 / core 2.46.5:

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_greenhouse_salary.py tests/integration/test_greenhouse.py --tb=short
38 passed, 28 subtests passed in 36.26s
```

No failures or skips. The tests cover oversized positive/negative integers in either bound; infinity, NaN, booleans, strings, null, negative and exclusive-maximum boundaries; retained valid ranges; description fallback; unknown salary; zero/fractional cents; and reversed ranges.

The real collector + synthetic SQLite regression verifies two imported jobs, two postings and two immutable snapshots, exact raw malformed metadata preservation, unchanged valid compensation, HEALTHY source state with zero failures/backoff, and successful durable readback from a reopened Store. Its only two requests pass through `httpx.MockTransport`; no provider network request or application submission occurs.

Additional checks on the implementation:

| Check | Result |
| --- | --- |
| Catalogue validation | PASS |
| Progress journal validation | PASS |
| Progress dashboard consistency | PASS |
| Git whitespace check | PASS |
| Source commit provenance, strict | PASS for all three source/test files |

## Review, release and limits

Author source/diff review found no remaining blocker in this scope. Independent review, protected hosted CI, actual merge-candidate verification, merge and production rollout are separate pending gates at this source checkpoint. Their final state belongs in the containing PR's receipts; this document does not predict them.

Live Greenhouse, PostgreSQL/Redis and browser journeys were NOT RUN locally. This backend normalization-only change requires no UI, migration or production-data test. Hosted required checks remain mandatory.

Rollback is a protected revert of the focused source change or the prior verified application image. No data restore or volume reset is required. No shared packages, Fleet controls, emergency cutoff, unrelated services or source acceptance totals were changed.
