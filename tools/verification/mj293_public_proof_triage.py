"""Read-only, privacy-minimal summary of MJ-REQ-292 public child proof outcomes.

Never fetches URLs, retries a child, changes fixtures, or promotes acceptance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def summarize_public_observations(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("public evidence must be an object")
    discovery = document.get("child_discovery")
    if not isinstance(discovery, dict):
        raise ValueError("missing child_discovery evidence")
    children = discovery.get("children")
    if not isinstance(children, list) or len(children) > 1000:
        raise ValueError("invalid bounded child evidence")
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("invalid child evidence")
        state, reason = child.get("state"), child.get("reason")
        if not isinstance(state, str) or not isinstance(reason, str):
            raise ValueError("missing child state/reason")
        # A fixed code allowlist prevents arbitrary provider text or identifiers
        # from being echoed into a published diagnostic.
        safe_states = {"BLOCKED", "COMPLETED", "FAILED", "CANCELLED", "QUEUED", "RUNNING"}
        safe_reasons = {"COMPANY_DOMAIN_CORROBORATION_REQUIRED", "BOOTSTRAP_DEADLINE",
                        "BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE", "SOURCE_BLOCKED",
                        "SOURCE_UNAVAILABLE", "DISCOVERY_AUTHORIZATION_CHANGED"}
        states[state if state in safe_states else "OTHER"] += 1
        reasons[reason if reason in safe_reasons else "OTHER"] += 1
    collection_state = document.get("collection_state")
    public_collection_state = (
        collection_state if isinstance(collection_state, str)
        and collection_state in {"COMPLETED", "BLOCKED", "FAILED"} else "UNKNOWN"
    )
    return {
        "child_count": len(children),
        "states": dict(sorted(states.items())),
        "reasons": dict(sorted(reasons.items())),
        "public_collection_state": public_collection_state,
        "employer_sources_enrolled": len(discovery.get("enrolled_sources", []))
            if isinstance(discovery.get("enrolled_sources"), list) else None,
        "enrolled_sources_present": isinstance(discovery.get("enrolled_sources"), list)
            and bool(discovery["enrolled_sources"]),
        "caveat": "Classification only; no official-domain or full acceptance credit",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public_evidence", nargs="+", type=Path)
    args = parser.parse_args()
    rows = []
    for path in args.public_evidence:
        with path.open(encoding="utf-8") as stream:
            document = json.load(stream)
        result = summarize_public_observations(document)
        result["input_index"] = len(rows) + 1
        rows.append(result)
    print(json.dumps({"schema": "MJ293_PUBLIC_PROOF_TRIAGE_V1", "runs": rows},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
