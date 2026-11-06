# Public-company response cleanup ? request outcome and socket ownership

Run: `web-20261105T024458Z-1b12b8fc-3b08-4780-ba5c-488849ee318d`.
Requirement: [MJ-REQ-004 / issue5](https://github.com/MulattoTech/MJobs/issues/5).
Scoped key: `PUBLIC-HTTP-RESPONSE-CLEANUP`.
Owner ref: `refs/heads/webpro-claims/PUBLIC-HTTP-RESPONSE-CLEANUP`, owner `b1b62a096035d62f96535bf3a4e5b4c3fee46c6c`.
Acquisition: HTTP201 plus exact readback at `2026-11-05T02:55:39.481300+00:00`.
Baseline: `d6650e4bc9bfd784bd6e3a346c793c372f02518a`.
Tested source commit: `758def9393832db78f014ac92334ef20bcb95354`.

## Reproduction and correction

In the monitored public-company request path, HTTPResponse.close ran before the
owned TLS/raw-socket cleanup loop. An injected expected OS-level close failure
replaced a successful response, cancellation, unavailable-control refusal,
deadline, policy refusal or fixed network-error result. Both socket objects could
remain registered until the client's outer cleanup. An unexpected response-close
error also skipped per-request disposal.

The unchanged baseline with the final regression scope reported **12 failing
assertions including five failed subcases; pytest displayed 12 failed /1 passed**,
exit1, 1.18s. Every added test method encountered a failure. The parent subtest
method's displayed pass is not accepted as a passing case when its subcases fail.
Two failures were exercised over real owned loopback TLS, not mock sockets alone.

The correction places socket disposal in a nested finally and suppresses only
expected OSError from response-file close. Successful response content and the
identity/classification of the primary outcome survive. Unexpected non-OS
response cleanup errors still propagate after socket cleanup is attempted.
Existing socket-close OSError handling, deadlines, rate policy, origins,
DNS/TLS verification, request limits and retry behavior are unchanged.

## Exact-source verification

Final command, using an isolated worktree/temp/output directory and read-only
existing Python3.12 dependencies:

```text
python -B -m pytest -q -p no:cacheprovider --basetemp <owned-temp> --junitxml <owned-receipt> tests/unit/test_public_company_response_cleanup.py tests/integration/test_public_company_response_cleanup.py tests/unit/test_public_company_client.py tests/integration/test_public_company_tls_policy.py tests/integration/test_company_monitoring_policy_transport.py --tb=short
45 passed, 18 subtests passed in 63.13s
exit0; zero skips
```

This covers the eight new methods plus existing transport, request-deadline,
cancellation, protocol/certificate/hostname, rate/concurrency, idempotent receipt
and reopened-database readback cases. The new TLS tests assert real descriptors
are closed and the client's socket registry is empty **before** client.close
can conceal missing per-request cleanup. Mocked response failures preserve the
exact cancellation/unavailable-control objects and fixed policy/network codes.

Tested file blobs:
- `services/company_discovery/public_client.py`: `30f902610db10b8b259ae1bd103b87a7340c3b27`.
- `tests/unit/test_public_company_response_cleanup.py`: `870e271eec19a4a5ac1f7c0f46919ceab9ad5613`.
- `tests/integration/test_public_company_response_cleanup.py`: `a87079206f21f5d7d3ce792b584b2e93ed6437da`.


New regression modules pass Ruff. The production file retains exactly the same
nine findings by code/message as baseline (one I001, one RUF059, six B023 and one
BLE001); no new finding or suppression was introduced. Two new-test import-order
findings were corrected before the final run. The first regression module import
also caused pytest to collect four imported TLS helper tests; it was changed to
a module import and the baseline rerun with only the intended new cases.
An earlier green45/18 run is retained locally; it is overlapping evidence and is
not added to the final totals.

Only synthetic data, ephemeral certificates and owned loopback sockets were used.
No public employer traffic, applicant data, real application, model invocation,
shared database or production change occurred.

## Limits, review and delivery

Author source review checked the complete diff, preserved exception identities,
owned-resource cleanup and privacy-safe diagnostics. Independent technical review,
fresh protected candidate CI, protected merge and deployed-user validation are
PENDING / NOT RUN at this source checkpoint. Same-author reinspection is not
independent review.

The change does not make arbitrary filesystem close calls preemptible, certify
all worker cleanup paths, suppress unexpected programming errors or establish
full MJ-REQ-004 acceptance. It preserves the prior non-monitoring response-close
behavior. Existing stored data and schemas are untouched; rollback is a protected
revert, assessed against intervening changes, without a data restore.

Original canonical statuses and denominators are unchanged; MJ-REQ-004 remains
PARTIAL. Current open PRs were inspected for the source/test paths before claim.
The broad004 owner is separately scoped to PR942 extraction. No heavy/browser
resource slot, integration lock or deployment lock was needed for this source
implementation.

Catalogue, canonical journal validation, dashboard freshness, scoped checkpoint validation, whitespace and strict source provenance all PASS at the source-evidence checkpoint. Hosted checks remain separate.
