## Purpose and MJobs requirements

<!-- State the issue/requirement and user-visible acceptance behavior. -->

## Reference provenance and third-party material

- Reference-derived change: [ ] No [ ] Yes
- MJobs issue/requirement IDs:
- Reference-project IDs and pinned commit(s), or reason there is no material inspiration:
- Material concepts informing the design:
- Third-party code/assets/prose proposed: [ ] No [ ] Yes
- License compatibility, attribution, NOTICE, modifications, and distribution review (if applicable):
- Third-party notices file updated if needed: [ ] Not needed [ ] Yes

## Safety and policy

- [ ] The implementation is clean-room by default and records material inspiration.
- [ ] No CAPTCHA solving, stealth/anti-bot evasion, fingerprint spoofing, MFA or credential bypass, access-control bypass, or rate-limit circumvention is introduced.
- [ ] `python -m tools.verification.reference_provenance` passes for reference-derived work.
- [ ] The candidate diff and required local evidence were reviewed; unavailable hosted evidence is reported as NOT_RUN.

## Validation and evidence

- [ ] Every new AI-authored source commit contains `AI-Agent`, `AI-Model`, `Requirement`, `Validation`, and `UTC-Date` trailers (actual values; use `unknown` if model identity is unavailable).
- [ ] `python -m tools.verification.commit_provenance --commit HEAD` passed for the AI-authored source commit, or the commit is explicitly human-authored.
- [ ] All affected unit/integration/browser checks list executed commands and honest PASS/FAIL/NOT_RUN outcomes; hosted checks remain disabled.

<!-- List exact local commands/results, evidence path, limitations, and recovery notes. -->

## Feature Studio specifications (when applicable)

- Specification ID and reviewed revision (or N/A):
- Specification lifecycle: DRAFT / REVIEW / APPROVED / IMPLEMENTING / VERIFIED / CLOSED
- User-visible acceptance criteria and exact commit-bound evidence:
- Affected API/contracts, dependencies, workstream conflicts, and rollback strategy:
- Required human approval and applicable security review:
- [ ] A schema/CI check is not misrepresented as completed feature acceptance.
- [ ] No automatic main pushes or unauthorized agents were introduced.
