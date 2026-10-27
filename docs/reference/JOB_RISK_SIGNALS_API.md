# Maintainer reference: profile-scoped job risk signals API

**Pinned source:** apps/api/job_risk_signals.py
**Source commit:** 2f01b876762fcbdd70f11e69865a48257503af93
**Source blob:** 884e074ea27bc1eb342e67f217cabd31eee2cc1a

## Purpose and public entry point

The module exports create_job_risk_signals_router(store, *, evaluator=None), which builds a FastAPI router. The router has one read endpoint:

    GET /profiles/{profile_id}/jobs/{job_id}/risk-signals

The main application mounts this router with its persistent store (apps/api/main.py, lines 762–763). Without an injected evaluator, the router lazily imports services.discovery.job_risk_signals.evaluate_job_risk. Passing evaluator is useful for a deterministic local test; the endpoint does not accept a request body (source lines 25–30, 136–143).

This is a profile-scoped, read-only advisory preview. It does not reject a job, alter match scores, write a risk decision, or make an external/provider request. The integration contract tests assert the database snapshot remains unchanged and explicitly block network connection attempts (test lines 114–124, 126–154).

## Scope and data flow

1. The handler loads the requested profile. Missing profile maps to HTTP 404 with PROFILE_NOT_FOUND (source lines 141–146).
2. It checks that the job has a match for this profile’s current version. A missing/stale/cross-profile match maps to 404 JOB_NOT_FOUND; a missing job or store ValueError also maps to that error (lines 147–161). The tests confirm cross-profile and nonexistent requests do not invoke the evaluator (test lines 156–166).
3. The evaluator receives a restricted current-job mapping containing only id, title, company, company_id, company_domain, description, attributes, application_url, field_provenance, salary bounds/currency/period, and company_identity. Application, document, audit, and lifecycle history records are intentionally not forwarded (source lines 163–173).
4. The API also supplies comparison_jobs, a tuple of at most 200 other jobs matched to the same profile version, excluding the current job and ordered by job id. The query selects basic job/company listing fields, not arbitrary history (lines 106–133, 174–179). Tests verify that another profile’s comparison job is excluded (test lines 146–151).
5. The service result is validated and translated. Each warning becomes a signal with code, fixed label Potential Risk, severity ADVISORY, bounded reason/evidence_source/observed_value. UNKNOWN service states become unknowns with code and reason; CLEAR states are not emitted as warnings or unknowns (source lines 40–103). Codes must have the required uppercase identifier form, signal codes must be unique, and warning codes must correspond exactly to WARN states; malformed or inconsistent results produce HTTP 502 RISK_SIGNAL_RESULT_INVALID (lines 14–18, 40–103, 179–180).

The JSON response shape is:

    {
      "profile_id": "fixture",
      "job_id": "fixture-risk-job",
      "signals": [
        {
          "code": "PAYMENT_REQUEST",
          "label": "Potential Risk",
          "severity": "ADVISORY",
          "reason": "Posting text requests an upfront payment.",
          "evidence_source": "posting.description",
          "observed_value": "pay a processing fee"
        }
      ],
      "unknowns": [
        {"code": "UNREALISTIC_SALARY", "reason": "SALARY_CURRENCY_OR_PERIOD_UNKNOWN"}
      ]
    }

The integration test gives this shape as a synthetic example (test lines 126–145).

## Safe local example

Use only an isolated local test store seeded with synthetic fixtures and a deterministic evaluator. The test suite’s pattern is to build a test FastAPI app, include the router with an injected evaluator, and issue a GET using TestClient:

    app.include_router(create_job_risk_signals_router(
        fixture_store, evaluator=synthetic_evaluator
    ))
    response = client.get(
        "/profiles/fixture/jobs/fixture-risk-job/risk-signals"
    )

This illustrates the API boundary only; this task did not run the test or make an HTTP request. See the exact fixture setup and local TestClient construction in tests/integration/test_job_risk_signals_api.py, lines 22–52.

## Failure behavior and limits

- 404 PROFILE_NOT_FOUND: profile lookup raises NoResultFound (source lines 143–146).
- 404 JOB_NOT_FOUND: no current-version profile match exists, or store.job raises NoResultFound/ValueError (lines 147–161).
- 502 RISK_SIGNAL_RESULT_INVALID: evaluator output is not a mapping, warnings/signals are not lists, an item/code/level/evidence is malformed, codes duplicate, or warning entries disagree with WARN signal states (lines 40–103). Tests cover malformed and inconsistent output (test lines 183–200).
- 503 RISK_EVALUATION_UNAVAILABLE: comparison lookup or evaluation/translation raises an exception other than the explicitly re-raised HTTPException (source lines 174–188). Tests cover an unavailable injected evaluator (test lines 202–208).

Failures remain advisory and do not mutate records; the integration tests compare database snapshots before/after error and success paths (test lines 126–154, 183–208). This source does not define an authentication policy; do not infer one from the profile-scoping check.

## Dependencies and existing documentation

The module depends on FastAPI routing/errors, SQLAlchemy select/NoResultFound, persistence table models, and the discovery evaluator (source lines 4–11, 25–30, 106–133). It uses a local store and has no HTTP client or external network dependency.

The API area guide describes other routes but does not mention risk-signals (apps/api/AREA.md, lines 1–38). The focused integration tests are the detailed behavioral contract (tests/integration/test_job_risk_signals_api.py, lines 126–230); unit tests cover the underlying evaluator separately (tests/unit/test_job_risk_signals.py, lines 36–176). No existing maintainer documentation found that explains this endpoint’s profile-version scope, comparison set, response translation, and error contract. This draft records existing source behavior only; reviewed tests were not executed for this task, and no acceptance or release claim is made.

## Evidence index

- API source: apps/api/job_risk_signals.py, lines 1–18, 21–37, 40–103, 106–133, 136–194.
- Router mount: apps/api/main.py, lines 762–763.
- API integration tests: tests/integration/test_job_risk_signals_api.py, lines 22–52, 114–124, 126–230.
- Evaluator tests: tests/unit/test_job_risk_signals.py, lines 36–176.
- Documentation coverage check: apps/api/AREA.md, lines 1–38.
- No tests or CI were run for this private reference draft.
