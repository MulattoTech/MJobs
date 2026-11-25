"""Canonical JUnit identities and explicit hosted-only skip allowances.

The name mapping mirrors the repository's pinned pytest 9.0.3 JUnit format.
Keep the aggregate job standard-library-only; test this mapping against the
actual pinned pytest collector whenever the pytest pin changes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree.ElementTree import Element

# Match the pinned reporter's XML display escaping, including its historical
# supplementary-character range behavior. Default pytest parameter IDs are escaped.
_XML_ILLEGAL = re.compile(
    r"[^\u0009\u000a\u000d\u0020-\u007e\u0080-\ud7ff\ue000-\ufffd\u10000-\u10ffff]"
)


def canonical_junit_identity(nodeid: str) -> tuple[str, str]:
    if not isinstance(nodeid, str) or not nodeid:
        raise ValueError("Missing collected test node ID")
    address, bracket, params = nodeid.partition("[")
    parts = address.split("::")
    path = parts[0]
    if (len(parts) < 2 or not path.startswith("tests/") or not path.endswith(".py")
            or "\\" in path or any(part in ("", ".", "..") for part in path.split("/"))
            or not all(parts[1:]) or (bracket and not params.endswith("]"))):
        raise ValueError("Invalid collected test node ID")
    classname = ".".join([path.removesuffix(".py").replace("/", "."), *parts[1:-1]])
    name = _XML_ILLEGAL.sub(lambda match: f"#x{ord(match.group()):02X}",
                            parts[-1] + bracket + params)
    return classname, name


def load_hosted_skips(root: Path, suite: str) -> dict[str, str]:
    """Only individually reviewed node IDs with their exact skip reason may skip."""
    policy = json.loads((root / "tools/ci/hosted_skip_contract.json").read_text(encoding="utf-8"))
    if (policy.get("schema_version") != 1
            or set(policy.get("suites", {})) != {"unit", "browser", "integration"}
            or suite not in policy["suites"]
            or not re.fullmatch(r"[0-9a-f]{40}", policy.get("baseline_head", ""))):
        raise ValueError("Invalid hosted skip contract")
    result: dict[str, str] = {}
    for group in policy["suites"][suite]:
        reason = group["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Missing reviewed skip reason")
        for nodeid in group["nodeids"]:
            canonical_junit_identity(nodeid)
            if not nodeid.startswith(f"tests/{suite}/") or nodeid in result:
                raise ValueError("Duplicate or out-of-suite hosted skip allowance")
            result[nodeid] = reason
    return result


def verify_hosted_skip(nodeid: str, case: Element, allowed: dict[str, str] | None) -> None:
    skipped = case.find("skipped")
    if skipped is not None and allowed is not None:
        if (skipped.get("type") != "pytest.skip" or nodeid not in allowed
                or skipped.get("message") != allowed[nodeid]):
            raise ValueError(f"Unexpected hosted skip: {nodeid}")
