# PR #938 current-main refresh verification

Requirement: MJ-REQ-171 / issue #172; frozen sitemap component PR #959.
Recorded UTC: 2026-11-06T05:25:57Z.

## Candidate and preservation

Original PR head: `9932d5f9a554467b0caa7b03ee2a39dbf084b045`.
Incoming main: `2621132e0a37c64d4b2a2b04960c91fc0cb49733`.
Tested merge tree: `e1921805d793ec9061b2cbedbab787e7d76aa71e`.
The final refresh adds this evidence file only to that tested tree. No executable or test inputs change afterward. The ordinary merge retains both parents and original author history. Frozen sitemap head `b6e3d982e7a8d404c5d70c1d91931278c5dfd146` is an ancestor; its five component source/test/evidence/handoff blobs are unchanged. Browser-profile/manual-auth source and owned tests are unchanged from the original PR head. Canonical merge content is retained without manual editing; scanner baseline comes unchanged from incoming main.

## Windows receipt

Python 3.12, native Windows; command:

```text
python -B -m pytest -q -rs tests/unit/test_browser_profile_configuration.py tests/unit/test_browser_profile_privacy.py tests/unit/test_manual_auth_deadline.py tests/unit/test_manual_auth_host.py tests/unit/test_manual_auth_path_security.py tests/unit/test_manual_auth_recovery.py tests/unit/test_career_page_sitemaps.py tests/integration/test_company_discovery_sitemap_safety.py
```

Result: **71 passed, 8 platform skips, 94 subtests passed in 15.50s**, exit 0.
Skips: configuration symlink and POSIX FIFO (2); privacy symlink and POSIX directory-mode cases (4); host and path-security POSIX refusal cases (2). These skips are not Linux acceptance.

## Bounded Linux receipt: PARTIAL

Existing image `mjobs-pr016-validation-cf86cd85:latest`, immutable ID `sha256:1a89f525e5ecce2209b8191a7f1a902011e804e87052d9450d4a5eba31a4a09d`, Python 3.12. Container used `--rm --pull never --network none --label ao.session=mjobs-6 --cpus 2 --memory 2g --read-only`, a read-only Git archive of the exact tested tree at `/workspace`, and a 512 MB `/tmp` tmpfs. No production mounts, credentials, package installation, or network submissions.

The initial four-module `python -B -m pytest -q -rs -p no:cacheprovider` attempt failed before collection: `No module named pytest`.
Fallback command:

```text
python -B -m unittest -v tests.unit.test_browser_profile_configuration tests.unit.test_browser_profile_privacy tests.unit.test_manual_auth_host tests.unit.test_manual_auth_path_security
```

Result: **26 successful tests plus 1 configuration-module import error**, 27 reported in 0.108s, exit 1. Privacy and both manual-auth modules executed successfully, including POSIX permission/refusal cases. Configuration is **NOT_RUN** because it imports unavailable `pytest`; overall Linux status is **PARTIAL**, not PASS. Successful cases were not rerun.

Static coverage check: `.github/workflows/ci.yml` runs `python -m pytest -q tests/unit --junitxml=outputs/ci/unit.xml` in its Ubuntu 24.04 Python job after installing `.github/test-requirements.txt`; `CI / required` depends on that job. Thus `tests/unit/test_browser_profile_configuration.py` is included. Fresh exact-head successful required Linux CI must cover this module before integration. Static inclusion is not an execution receipt.

## Old scanner disposition and remaining gates

Old run `41962916836` had one redacted `generic-api-key` finding in historical `planning/SESSION_HANDOFF.md:440`, commit `0bfeeb458eb32b7de3695dd754a09bcd6304126e`. Its exact fingerprint matches the reviewed main disposition introduced by `9380be70f8f85b5ea82491a54a0630af692234e9`: a verified public Docker image ID. No matched value is recorded here and no new ignore entry or dismissal was authored. This mapping is not fresh scanner PASS.

Old integration run `41962916731` failed AI TLS cancellation staging and Greenhouse TCP deadline cases outside this lane. Their repair remains with assigned owners; this receipt does not declare them resolved.

Cached diff check passed. Fresh hosted CI, fresh secret scan, native review, merge, deployment, real authenticated browser use, and whole-requirement acceptance are NOT_RUN or pending separately. Runtime slot released. Publication requires the primary's serial slot.
