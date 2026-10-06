# Reference implementation provenance and license-safe use

This policy governs implementation work inspired by public repositories in
[`REFERENCE_PROJECTS.json`](REFERENCE_PROJECTS.json). It applies regardless of
whether the MJobs change is user-visible, internal, or described as a small slice.

## Default: clean-room concepts

- Use public projects to identify workflows, failure modes, and architectural
  ideas. Describe the needed behavior in MJobs terms and write against MJobs
  requirements, interfaces, and acceptance tests.
- Do not paste, translate, port, or adapt third-party source code, tests, prompts,
  documentation prose, or assets by default. Public availability is not permission.
- Before implementation of a proposed direct incorporation starts, pin the exact source
  repository, commit/ref, file, and license. Review license compatibility,
  attribution, copyright/NOTICE and patent obligations, modification notices, and
  distribution scope and required third-party notices. If compatibility or permission is unclear, stop that source
  reuse and continue with a clean-room implementation.
- `AutoApply` is inspected as inspiration only under PolyForm Noncommercial
  License 1.0.0. MJobs does not copy, modify, derive, vendor, or distribute its
  source/assets. Commercial use is not granted by the inspected license.
- For AIHawk / `invisible_playwright_mcp`, use the exact history row in the
  registry: earlier copies were AGPL-3.0; the later MIT relicense does not change
  the terms for already distributed copies or forks.

## Prohibited behavior

No license grants permission to import or implement behavior whose purpose is
stealth or evasion. Never import CAPTCHA solving or outsourcing, anti-bot
evasion, fingerprint spoofing, proxy/block circumvention, MFA bypass, credential
bypass, access-control bypass, or rate-limit circumvention. When a site requires
user action, preserve the manual intervention/review boundary.

## Material-inspiration record

For each material reference-derived behavior, update
[`REFERENCE_INSPIRATION_LOG.md`](REFERENCE_INSPIRATION_LOG.md) and the issue,
design note, or change description with:

1. stable MJobs issue/requirement ID;
2. reference-project ID, repository URL, inspected commit/ref/date, and license;
3. the specific concept or workflow that informed the design;
4. the MJobs-specific acceptance oracle and independent implementation approach;
5. whether any third-party code/assets are proposed. If yes, attach the exact-file
   license and required notice review before source is introduced.

Do not infer code provenance from similar behavior alone. Record the cited
design source and whether direct code incorporation was intentionally proposed.

## Pre-merge / pre-integration checklist

Complete this checklist in the pull-request description or local integration
evidence for reference-derived work:

- [ ] The MJobs issue/requirement and every material inspiration are identified.
- [ ] Each source is pinned to its inspected repository, commit/ref/date, and
  exact license evidence in the registry.
- [ ] The change describes independently written MJobs behavior and acceptance;
  it does not include pasted/adapted source, tests, prompts, prose, or assets.
- [ ] If third-party material is intentionally proposed, a compatible license,
  required attribution/NOTICE, modifications, and distribution scope were
  reviewed before the material entered MJobs.
- [ ] CAPTCHA, stealth/anti-bot evasion, fingerprint spoofing, MFA, credential,
  access-control, proxy/block, and rate-limit circumvention remain prohibited.
- [ ] `python -m tools.verification.reference_provenance` passes. The validator
  checks the registry and checklist artifacts; it does not replace review of the
  candidate diff or claim legal approval.
- [ ] `THIRD_PARTY_NOTICES.md` is updated if third-party code/assets are
  intentionally added.

If any required answer is unknown, stop integration until the source/use posture
is resolved or the change is rewritten clean-room.
