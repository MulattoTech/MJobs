# Recovery bundle and archive-reference audit — issue #525

## Disposition and scope

**PARTIAL — not security-ready certification and not issue closure.**
Audit date: 2026-10-25 UTC. Source baseline:
`3dde716311194f6f1fa9c892816818796d7652b2`.
Exclusive change: this document. Production, scanner policy, ignore fingerprints,
Git history, public refs and private recovery copies were not modified.

The [full issue and owner receipt](https://github.com/MulattoTech/MJobs/issues/525#issuecomment-6111632253)
report that two bundles were privately uploaded, downloaded and SHA-256 verified
before retiring the public historical release and twelve recovery/archive tags.
The reported scan covered 2,790 commits and classified 67 candidates as metadata,
Git IDs or explicit test mocks. These are **historical owner attestations**, not
new recovery verification performed by this audit. No private location, manifest,
candidate value, screenshot or credential is reproduced here.

## Threat boundary and current observations

An anonymous downloader could have retained previously public bundles, tags,
issue attachments or screenshots. Removing discovery pointers does not revoke
credentials or recall downloaded data. A currently advertised ref inventory is
not an inventory of all reachable Git objects, caches, forks or retained clones.

| Surface | Observation / evidence | Coverage limitation |
| --- | --- | --- |
| GitHub releases | Paginated REST `repos/MulattoTech/MJobs/releases` returned zero entries on the audit date. | No binary download or inspection; does not establish erasure from caches. |
| Historical archive tags | REST `git/matching-refs/tags/archive/mjobs/20261010/` returned zero entries. | Only the named prefix; no mass deletion performed. |
| Historical recovery tag | REST `git/matching-refs/tags/mjobs-history-backup-20261010` returned zero entries. | Does not assess semantic product tags or unreachable objects. |
| Private recovery | Owner receipt above and [maintainer status](../maintainers/MODERNIZATION_STATUS.md). | Private bundles/manifests unavailable to this audit; no present-day integrity/restore claim. |
| Scanner wrapper | [secret_scan.py](../../tools/ci/secret_scan.py) pins download digest, limits download/member size, extracts only a regular `gitleaks` member, and invokes `git --log-opts=--all --redact`. | Local refs only; cannot recover retired remote objects absent from checkout. No detector execution here. |
| Public CI | [workflow](../../.github/workflows/secret-scan.yml) uses read-only permission, pinned actions, full-depth checkout and a failure-only report artifact retained three days. | Checkout full depth does not demonstrate possession of all retired/private refs. Redaction is delegated to Gitleaks, not independently checked by the wrapper. |
| Ignore policy | Exact fingerprint input is `.gitleaksignore`; [scanner configuration](../../.github/secret-scanner.json) pins version/archive/SHA-256. | Prior classifications were not individually re-adjudicated; this audit adds no exceptions. |
| Broader content | Issue #525, SECURITY.md, maintainer status, scanner/config/workflow reviewed. | Entire history, all issues/comments, evidence, screenshots and licensing provenance are **NOT_RUN**. No comprehensive PII or copyright clearance. |

The optional `git ls-remote` probe failed with a local Windows Schannel credential
error; the successful REST probes above are the visibility evidence instead.
No credential repair or global configuration change was attempted.

## Repeatable, synthetic regression evidence

This probe tests the real wrapper with synthetic archive bytes and mocked I/O.
It never downloads a scanner, executes the dummy executable, scans real secrets,
reads a private bundle, or publishes an artifact. The digest rejection case must
stop before process invocation; a rejected scanner result must remain nonzero.
The command assertions pin full-ref traversal, redaction and the exact ignore
input. They **do not prove Gitleaks actually redacts every sensitive field**.

Save this block to a temporary file outside the checkout and execute from the
repository root with its path added to `PYTHONPATH` (the repository root, not the
temporary directory). Expected: three tests, including two subprocess outcomes.

```python
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from tools.ci import secret_scan as scanner


class RecoveryScannerContract(unittest.TestCase):
    def test_checksum_mismatch_never_invokes_scanner(self):
        self.exercise(bad_digest=True)

    def test_success_and_failure_preserve_redacted_command(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                self.exercise(returncode=returncode)

    def test_nonregular_member_never_invokes_scanner(self):
        self.exercise(nonregular=True)

    def exercise(self, bad_digest=False, returncode=0, nonregular=False):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as package:
            member = tarfile.TarInfo('gitleaks')
            data = b'synthetic executable placeholder; never executed'
            if nonregular:
                member.type = tarfile.SYMTYPE
                member.linkname = 'outside'
                package.addfile(member)
            else:
                member.size = len(data)
                package.addfile(member, io.BytesIO(data))
        payload = archive.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.github').mkdir()
            digest = '0' * 64 if bad_digest else hashlib.sha256(payload).hexdigest()
            (root / '.github/secret-scanner.json').write_text(json.dumps({
                'version': '0.0.0', 'archive': 'synthetic.tar.gz', 'sha256': digest
            }), encoding='utf-8')
            with patch.object(scanner, 'ROOT', root), \
                 patch.object(scanner.platform, 'system', return_value='Linux'), \
                 patch.object(scanner.platform, 'machine', return_value='x86_64'), \
                 patch.object(scanner, 'urlopen', return_value=io.BytesIO(payload)), \
                 patch.object(scanner.subprocess, 'run') as run:
                run.return_value.returncode = returncode
                if bad_digest or nonregular:
                    with self.assertRaises(RuntimeError):
                        scanner.main()
                    run.assert_not_called()
                    return
                self.assertEqual(scanner.main(), returncode)
                args = run.call_args.args[0]
                self.assertEqual(args[1:3], ['git', str(root)])
                self.assertIn('--log-opts=--all', args)
                self.assertIn('--redact', args)
                self.assertEqual(args[args.index('--report-path') + 1],
                                 str(root / 'outputs/ci/secret-scan-redacted.json'))
                self.assertEqual(args[args.index('--gitleaks-ignore-path') + 1],
                                 str(root / '.gitleaksignore'))
                self.assertEqual(run.call_args.kwargs['timeout'], 600)
                self.assertFalse((root / 'outputs/ci/secret-scan-redacted.json').exists())


if __name__ == '__main__':
    unittest.main()
```

## Visibility/remediation plan — proposed, owner decision outstanding

1. Preserve the existing private copies and manifests. Owner should privately
   reconfirm hashes, ref manifest completeness, custody/access restrictions and
   a safe offline restore of valuable unmerged work. Publish only a sanitized
   completion receipt, never storage paths or recovery data.
2. Inventory the remaining content surfaces privately: complete archive object
   graph, current history, issues/comments/attachments, evidence, release metadata,
   screenshots and licensing provenance. Record reviewed ranges, exclusions,
   reviewer and timestamps. Pattern scans alone do not cover these surfaces.
3. For a real exposed credential, privately notify the owner and revoke/rotate
   first through the provider. Preserve restricted forensic evidence; do not
   paste the credential into issues, logs, CI artifacts or this document.
4. For personal data or unlicensed material, request owner/legal disposition
   using sanitized categories and private references. Assess derivatives and
   retained downloads; never promise recall or deletion beyond verified control.
5. Any additional public asset/tag removal or history rewrite requires a fresh,
   explicit owner decision after verified private recovery. Preserve semantic
   product tags separately from archive markers. No deletion is requested here.
6. Owner must approve the visibility/remediation plan and remaining coverage
   before #525 can close. The existing receipt authorizes neither new destructive
   actions nor a broad security-ready claim. See [security policy](../../SECURITY.md).

No production change is proven necessary by this bounded audit. Persistent
regression automation would require a separate lease for scanner tests; this
Markdown probe is reproducible evidence, **not a CI-discovered test module**.

## Validation ledger

Results below are local, synthetic and limited to this document/source baseline.
The PR body records the final document commit and live exact-head hosted checks;
missing/pending hosted checks must remain NOT_RUN/PENDING, never inherited from
#781 or another revision. Full private archive and comprehensive privacy/licensing
acceptance remain PARTIAL regardless of CI outcomes.

- Embedded synthetic scanner probe: **3 tests PASS** (checksum mismatch,
  nonregular archive member, and success/failure command propagation with two
  subcases). The printed scanner `FAIL` is the deliberately mocked nonzero case.
- `python -m pytest -q -p no:cacheprovider tests/unit/test_public_ci_tools.py`:
  **10 passed**.
- `python -m tools.ci.check_repository`: **PASS, 0 errors**.
- `git diff --check`: **PASS** (rechecked after staging the new document).
- Actual Gitleaks detector/private archive scan: **NOT_RUN**; this Windows host
  cannot run the pinned Linux x64 wrapper unchanged. No binary was installed.
- Fresh owner approval of the proposed plan: **OUTSTANDING**.
- Existing site build via local Python environment: **PASS**, 7 pages and 3
  reviewed screenshots; no runtime export. This is not a visual review of
  historical screenshots and does not clear their privacy or licensing.
- Runtime for probe and CI-helper tests: Python 3.12.10 on Windows.
