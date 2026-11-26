"""Fail-closed exact collection/JUnit parity and cross-shard CI reconciliation."""
from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.ci.integration_split import SPLIT_NODES, invocation
from tools.ci.junit_identity import canonical_junit_identity, load_hosted_skips, verify_hosted_skip

TEMPLATE_TARGET = "tests/integration/test_aggregator_bootstrap_postgres.py"
PROOF_NAME = "postgres-template-proof.json"
ROOT = Path(__file__).resolve().parents[2]


def _unique(values: list[str]) -> set[str]:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Missing test identity")
    result = set(values)
    if len(result) != len(values):
        raise ValueError("Duplicate test identity")
    return result


def verify_template_proof(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Requested PostgreSQL template proof is missing")
    proof = json.loads(path.read_text(encoding="utf-8"))
    if (not re.fullmatch(r"[0-9a-f]{64}", proof.get("full_dump_sha256", ""))
            or proof.get("schema_data_owner_acl_equivalence") is not True
            or proof.get("cross_clone_and_future_clone_isolation") is not True):
        raise ValueError("PostgreSQL template equivalence or isolation is not proved")
    clones = proof.get("clone_seconds")
    if not isinstance(clones, list) or len(clones) != 2:
        raise ValueError("Missing PostgreSQL clone timings")
    times = [*clones, *(proof.get(name) for name in (
        "fresh_migration_seconds", "template_initialization_seconds", "total_proof_setup_seconds",
    ))]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in times):
        raise ValueError("Invalid PostgreSQL template proof timings")


def verify_shard(manifest: dict, report: Path, targets: list[str], *,
                 split: bool, postgres_template: bool = False,
                 allowed_skips: dict[str, str] | None = None) -> tuple[set[str], set[tuple[str, str]]]:
    expected_targets = _unique(targets)
    _, exclusions = invocation(targets) if split else (targets, [])
    inventory = _unique(manifest["inventory"])
    excluded = _unique(manifest["excluded"])
    selected = _unique([row["nodeid"] for row in manifest["selected"]])
    completed = _unique(manifest["completed"])
    if (excluded != set(exclusions) or not excluded <= inventory
            or not selected or selected != inventory - excluded or completed != selected):
        raise ValueError("Shard collection or completion mismatch")
    owned_targets = {node if split and node in SPLIT_NODES else node.split("::", 1)[0]
                     for node in selected}
    if owned_targets != expected_targets:
        raise ValueError("Shard collected tests outside its exact assigned targets, or omitted targets")
    expected = {}
    for row in manifest["selected"]:
        identity = canonical_junit_identity(row["nodeid"])
        if identity != (row["classname"], row["name"]):
            raise ValueError("Collected node ID does not match JUnit identity")
        if identity in expected:
            raise ValueError("Missing or duplicate collected JUnit identity")
        expected[identity] = row["nodeid"]
    actual = set()
    for case in ET.parse(report).iter("testcase"):
        identity = (case.get("classname"), case.get("name"))
        if identity in actual or identity not in expected:
            raise ValueError("Unexpected or duplicate JUnit identity")
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError("Failed shard test")
        if (postgres_template and expected[identity].split("::", 1)[0] == TEMPLATE_TARGET
                and case.find("skipped") is not None):
            raise ValueError("Requested PostgreSQL template fixture was skipped")
        verify_hosted_skip(expected[identity], case, allowed_skips)
        actual.add(identity)
    if actual != set(expected):
        raise ValueError(f"Missing exact JUnit identities: {len(set(expected) - actual)}")
    if postgres_template and TEMPLATE_TARGET in expected_targets:
        verify_template_proof(report.parent / PROOF_NAME)
    return selected, actual


def reconcile(directory: Path, suite: str, count: int, *, postgres_template: bool = False,
              hosted: bool = False, root: Path = ROOT) -> int:
    from tools.ci.run_shard import select_files

    if count < 1 or suite not in ("browser", "integration"):
        raise ValueError("Invalid shard matrix")
    allowed_skips = load_hosted_skips(root, suite) if hosted else None
    split = suite == "integration" and (root / "tools/ci/integration_timings.json").is_file()
    all_targets = set(select_files(root, suite, 0, 1))
    seen_targets = set()
    seen_nodes = set()
    seen_identities = set()
    names = {f"{suite}-results-{index}" for index in range(count)}
    if {path.name for path in directory.iterdir()} != names:
        raise ValueError("Missing or unexpected shard artifact directory")
    for index in range(count):
        folder = directory / f"{suite}-results-{index}"
        manifest = json.loads((folder / f"{suite}-{index}-collection.json").read_text(encoding="utf-8"))
        targets = select_files(root, suite, index, count)
        if ((manifest["suite"], manifest["index"], manifest["count"], manifest["targets"])
                != (suite, index, count, targets) or seen_targets & set(targets)):
            raise ValueError("Shard metadata or assignment mismatch")
        if (folder / PROOF_NAME).exists() != (postgres_template and TEMPLATE_TARGET in targets):
            raise ValueError("Missing or unexpected PostgreSQL template proof owner")
        nodes, identities = verify_shard(manifest, folder / f"{suite}-{index}.xml", targets,
                                         split=split, postgres_template=postgres_template,
                                         allowed_skips=allowed_skips)
        if seen_nodes & nodes or seen_identities & identities:
            raise ValueError("Duplicate test across shards")
        seen_targets.update(targets)
        seen_nodes.update(nodes)
        seen_identities.update(identities)
    if seen_targets != all_targets:
        raise ValueError("Incomplete matrix target inventory")
    return len(seen_nodes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--suite", choices=["browser", "integration"], required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--postgres-template", action="store_true")
    parser.add_argument("--hosted", action="store_true", help="Enforce reviewed hosted skip contract")
    args = parser.parse_args()
    if args.postgres_template and args.suite != "integration":
        parser.error("Template proof belongs only to integration")
    print(f"Verified {reconcile(args.directory, args.suite, args.count, postgres_template=args.postgres_template, hosted=args.hosted)} exact test identities across all shards")


if __name__ == "__main__":
    main()
