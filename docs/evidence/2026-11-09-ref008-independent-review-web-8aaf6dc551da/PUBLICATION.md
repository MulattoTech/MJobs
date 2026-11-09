# PR #991 independent review — publication and rerun guide

## Recorded outcome and scope

The independent review tested **MJ-REF-008 / issue #378 / PR #991** at:

- Head: `ca6a66fae084d4254c8044e2edb774b6e5a4a9b5`
- Tree: `3eb631a9a16a731aebbbed0b3b9197427c0140f2`
- Base: `971ba931418fc271e7a652e27c288e1776448b6d`

The six affected repository test modules passed **25 tests and 131 subtests**. The separate independent probes passed **7 tests and 62 subtests**. Both commands exited **0**. No blocking finding was confirmed in that scope.

This evidence supports approved-source grounding, source references, explicit missing-evidence handling, immutable profile/job pack versioning, sanitized scope errors, and deterministic backend export using synthetic SQLite/ASGI fixtures. Independent probes include all five persisted content comparison fields, approval revocation, unselected private facts, same-name company identity changes, and additional Unicode/identifier boundaries.

**NOT_RUN by this review:** browser interactions, deployed/live runtime acceptance, PostgreSQL-specific concurrent pack creation, full repository suite, frontend build/Vitest, and hosted-check reexecution. Literal matching does not establish negation, aliases, seniority, paraphrases or general qualification entailment. There is **no fresh whole-MJ-REF-008 PASS certification or downstream acceptance promotion**.

The statements in the original report that the probes were “not added to the repository or a PR” describe **review execution**: the probes ran outside the detached target checkout, with no application-source or existing-test edits. A separate evidence-only branch may subsequently preserve these unchanged artifacts. Such publication does not alter the tested target revision, create a product change, or imply merge/deployment authority.

## Minimal publication bundle

Publish these seven UTF-8 files together in the root agent's claimed run-specific `docs/evidence/` directory. Preserve the six pre-existing execution files byte for byte.

| File | Purpose |
| --- | --- |
| `PUBLICATION.md` | This scope, publication and portable rerun guide |
| `review.md` | Criterion ledger, findings, commands and limitations |
| `receipt.json` | Recorded exact head, command arguments, exit codes and artifact hashes |
| `test_ref008_independent.py` | Runnable independent synthetic probes |
| `source-hashes.json` | SHA-256 and Git blob identities for examined repository files |
| `affected-exact-source.log` | Successful six-module repository test output |
| `independent.log` | Successful independent probe output |

The minimal bundle omits the original environment inventory, GitHub metadata snapshot, JUnit XML and preliminary dependency-collection logs. The unchanged receipt/report retain references to those supplementary execution artifacts. Their presence is not required to run the published probes or verify the six preserved files. Do not rewrite the historical receipt to remove those references. All probe identities and records are fictional; the bundle contains no credentials or real applicant records. Original cloud scratch paths are historical command context, not rerun prerequisites.

### Verify the preserved artifacts

Run this from the directory containing the published files:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib

expected = {
    "review.md": "14b1f04d6e441947143adb65a5bae69fe8676e9fb64bc3797c7878abf1e0d67c",
    "receipt.json": "9abbbe6c76a58b80bc044e0a828862a8fcf4de27e862ee9d7194feed69e75a9f",
    "test_ref008_independent.py": "7d0b1c98774e3f3d236d9f37d2807f737217154aaa685f15fee8c83f945a7ba2",
    "source-hashes.json": "160ee9a8bb08af5331156c679c1a0edc49a07df5837732829bafed8dd9f646a6",
    "affected-exact-source.log": "1c3744700c1df139cfde6ae43dc175c66e2437e8b1fcc0e3148b3b30361a5ffd",
    "independent.log": "20fbec3e44aab815d8554daad50fac81568802801ae771f80f2469186eda64b2",
}
for name, wanted in expected.items():
    data = Path(name).read_bytes()
    data.decode("utf-8")
    actual = hashlib.sha256(data).hexdigest()
    if actual != wanted:
        raise SystemExit(f"Hash mismatch: {name}")
    print(f"OK {name}")
PY
```

This guide's identity is established by its publication Git commit; it does not contain a recursive hash of itself.

## Portable rerun

Prerequisites: Git, Bash, Python **3.12**, and access to the repository and the pinned Python packages. The tests use temporary SQLite databases and ASGI clients; PostgreSQL, Redis servers, browsers and provider credentials are not prerequisites for these commands.

Start in the published evidence directory. The commands below create a separate temporary checkout and virtual environment and write new JUnit reports outside the preserved bundle.

### 1. Obtain the exact tested source

```sh
set -euo pipefail
REF008_EVIDENCE="$(pwd)"
REF008_HEAD="ca6a66fae084d4254c8044e2edb774b6e5a4a9b5"
REF008_RUN="$(mktemp -d -t mjobs-ref008-review.XXXXXX)"
REF008_TARGET="$REF008_RUN/source"
REF008_PY="$REF008_RUN/venv/bin/python"

git clone --no-checkout https://github.com/MulattoTech/MJobs.git "$REF008_TARGET"
git -C "$REF008_TARGET" fetch origin "$REF008_HEAD"
git -C "$REF008_TARGET" checkout --detach "$REF008_HEAD"
test "$(git -C "$REF008_TARGET" rev-parse HEAD)" = "$REF008_HEAD"
test "$(git -C "$REF008_TARGET" rev-parse 'HEAD^{tree}')" = "3eb631a9a16a731aebbbed0b3b9197427c0140f2"
```

Keep the same shell for the following commands. The evidence-publication branch is not the test target.

### 2. Verify the source files and install pinned prerequisites

```sh
python3 - "$REF008_EVIDENCE" "$REF008_TARGET" <<'PY'
from pathlib import Path
import hashlib
import json
import subprocess
import sys

evidence, target = map(Path, sys.argv[1:])
hashes = json.loads((evidence / "source-hashes.json").read_text())
for relative, expected in hashes.items():
    actual = hashlib.sha256((target / relative).read_bytes()).hexdigest()
    if actual != expected["sha256"]:
        raise SystemExit(f"Source hash mismatch: {relative}")
    blob = subprocess.check_output(
        ["git", "-C", str(target), "rev-parse", f"HEAD:{relative}"],
        text=True,
    ).strip()
    if blob != expected["git_blob"]:
        raise SystemExit(f"Git blob mismatch: {relative}")
print(f"Verified {len(hashes)} source file identities")
PY

python3.12 -m venv "$REF008_RUN/venv"
"$REF008_PY" -m pip install \
  -c "$REF008_TARGET/requirements.txt" \
  -c "$REF008_TARGET/.github/test-requirements.txt" \
  pytest PyYAML SQLAlchemy alembic fastapi httpx pydantic opentelemetry-api \
  redis rq croniter beautifulsoup4 python-docx reportlab pypdf cryptography \
  lxml python-multipart python-dateutil uvicorn
"$REF008_PY" -m pip check
cd "$REF008_TARGET"
```

The two constraint files are read from the immutable target checkout. Pytest 9.0.3 supplies the subtest reporting used by this run. Redis and Uvicorn are required imports for the selected repository modules; no server is launched. The original review initially lacked those imports and recorded collection failures before installing the pinned dependencies.

### 3. Run the repository modules

```sh
PYTHONDONTWRITEBYTECODE=1 "$REF008_PY" -m pytest -q -p no:cacheprovider \
  tests/unit/test_interview_preparation.py \
  tests/integration/test_interview_preparation_api.py \
  tests/integration/test_interview_preparation_scope_and_no_write.py \
  tests/integration/test_interview_preparation_router_mount.py \
  tests/integration/test_interview_preparation_migration.py \
  tests/integration/test_interview_preparation_migration_constraints.py \
  --junitxml="$REF008_RUN/affected-rerun.xml"
```

Recorded result to reproduce: **25 passed, 131 subtests passed, exit 0**.

### 4. Run the unchanged independent probes

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REF008_TARGET" \
  "$REF008_PY" -m pytest -q -p no:cacheprovider \
  "$REF008_EVIDENCE/test_ref008_independent.py" \
  --junitxml="$REF008_RUN/independent-rerun.xml"
```

Recorded result to reproduce: **7 passed, 62 subtests passed, exit 0**. The original successful runs each emitted one TestClient deprecation warning about future HTTPX2 use; timings are not acceptance assertions.

Keep new rerun reports separate from the preserved evidence. These instructions adapt the already executed commands to portable paths; preparing this guide did not rerun the suites, change their historical results, or execute the additional NOT_RUN checks.

