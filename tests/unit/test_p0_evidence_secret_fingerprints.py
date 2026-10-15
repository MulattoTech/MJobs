"""Reviewed digest exceptions stay bound to immutable historical findings."""
from collections import Counter
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_REVISION = "a3551f268e8cadb4152068ca83c1357e71417154"
EXACT_FINGERPRINT = re.compile(r"[0-9a-f]{40}:[^:\r\n*?]+:[a-z0-9-]+:[1-9][0-9]*")


def fingerprints():
    return [line.strip() for line in (ROOT / ".gitleaksignore").read_text(
        encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_original_p0_digest_review_covers_only_76_exact_historical_findings():
    reviewed = [line for line in fingerprints() if line.startswith(EVIDENCE_REVISION + ":")]
    assert len(reviewed) == len(set(reviewed)) == 76
    assert all(EXACT_FINGERPRINT.fullmatch(line) for line in reviewed)
    assert Counter(line.split(":")[1] for line in reviewed) == {
        "docs/evidence/2026-10-15-headless-development-full-criterion.json": 75,
        "docs/evidence/2026-10-15-controlled-concurrency-full-criterion.json": 1,
    }
    assert {line.split(":")[2] for line in reviewed} == {"generic-api-key"}


def test_ignore_file_has_no_rule_path_or_wildcard_exemptions():
    assert all(EXACT_FINGERPRINT.fullmatch(line) for line in fingerprints())
    for broad in ("generic-api-key", "docs/evidence/*", "*:path:generic-api-key:1",
                  EVIDENCE_REVISION + ":path:generic-api-key:*"):
        assert EXACT_FINGERPRINT.fullmatch(broad) is None
