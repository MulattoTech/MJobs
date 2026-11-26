"""Exact, isolated scheduling of expensive integration cases; never shorten their waits."""
from __future__ import annotations

from pathlib import Path

RECOVERY_FILE = 'tests/integration/test_rq_workhorse_recovery.py'
RECOVERY_CLASS = 'RqWorkhorseRecoveryTests'
RECOVERY_CASES = (
    'test_abandoned_execution_maintenance_redelivers_to_fresh_production_workhorse',
    'test_abandoned_mock_submit_with_unavailable_receipt_remains_uncertain_without_resubmission',
)
SPLIT_NODES = tuple(f'{RECOVERY_FILE}::{RECOVERY_CLASS}::{case}' for case in RECOVERY_CASES)
SPLIT_IDENTITIES = {
    (f'{RECOVERY_FILE.removesuffix(".py").replace("/", ".")}.{RECOVERY_CLASS}', case): node
    for case, node in zip(RECOVERY_CASES, SPLIT_NODES, strict=True)
}


def target_weights(files: list[str], timings: dict[str, float],
                   case_timings: dict[str, float]) -> tuple[list[str], dict[str, float]]:
    """Keep every whole-file remainder; schedule known expensive cases independently."""
    targets = list(files)
    weights = dict(timings)
    if RECOVERY_FILE in files:
        targets.extend(SPLIT_NODES)
        for node in SPLIT_NODES:
            weights[node] = case_timings.get(node, 152.0)
        weights[RECOVERY_FILE] = max(0.001, timings.get(RECOVERY_FILE, 305.0)
                                     - sum(weights[node] for node in SPLIT_NODES))
    return targets, weights


def invocation(targets: list[str]) -> tuple[list[str], list[str]]:
    """A shard owning the remainder runs the whole file minus exactly unowned cases."""
    selected = set(targets)
    excluded = [node for node in SPLIT_NODES if RECOVERY_FILE in selected and node not in selected]
    paths = [target for target in targets
             if not (target in SPLIT_NODES and RECOVERY_FILE in selected)]
    return paths, excluded


def target_for_case(file: str, classname: str, name: str) -> str:
    return SPLIT_IDENTITIES.get((classname, name), file)


def target_file(target: str) -> str:
    return Path(target.split('::', 1)[0]).as_posix()
