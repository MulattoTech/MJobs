"""Compare complete integration JUnit inventories without publishing payloads."""
from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def read_inventory(directory: Path, expected_shards: int | None = None) -> dict[tuple[str, str], str]:
    """Reject duplicate test identities, missing reports and invalid outcomes."""
    reports = sorted(directory.rglob('integration-*.xml'))
    if not reports:
        raise ValueError(f'No integration reports in {directory}')
    if expected_shards is not None:
        expected = {f'integration-{i}.xml' for i in range(expected_shards)}
        if expected_shards < 1 or {p.name for p in reports} != expected or len(reports) != expected_shards:
            raise ValueError('Missing, unexpected or duplicate shard reports')
    inventory = {}
    for report in reports:
        cases = list(ET.parse(report).iter('testcase'))
        if not cases:
            raise ValueError(f'Empty report: {report.name}')
        for case in cases:
            key = (case.get('classname', ''), case.get('name', ''))
            if not all(key) or key in inventory:
                raise ValueError(f'Missing or duplicate test identity: {key}')
            flags = [flag for flag in ('failure', 'error', 'skipped') if case.find(flag) is not None]
            if len(flags) > 1:
                raise ValueError(f'Conflicting test outcomes: {key}')
            inventory[key] = flags[0] if flags else 'passed'
    return inventory


def compare_inventories(
    baseline: dict[tuple[str, str], str], candidate: dict[tuple[str, str], str],
) -> dict:
    """Report inventory parity and regressions, including newly skipped tests."""
    removed = sorted(baseline.keys() - candidate.keys())
    added = sorted(candidate.keys() - baseline.keys())
    changes = sorted(key for key in baseline.keys() & candidate.keys()
                     if baseline[key] != candidate[key])
    new_skips = sorted(key for key, outcome in candidate.items()
                       if outcome == 'skipped' and baseline.get(key) != 'skipped')
    unsuccessful = sorted(key for key, outcome in candidate.items() if outcome in {'failure', 'error'})
    return {
        'baseline': dict(Counter(baseline.values())),
        'candidate': dict(Counter(candidate.values())),
        'baseline_count': len(baseline),
        'candidate_count': len(candidate),
        'removed': removed,
        'added': added,
        'outcome_changes': changes,
        'new_skips': new_skips,
        'failures_or_errors': unsuccessful,
        'inventory_parity_pass': bool(baseline and candidate) and not (
            removed or new_skips or unsuccessful
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--baseline-shards', type=int, required=True)
    parser.add_argument('--candidate-shards', type=int, required=True)
    parser.add_argument('--baseline-wall-seconds', type=float)
    parser.add_argument('--candidate-wall-seconds', type=float)
    args = parser.parse_args()
    comparison = compare_inventories(
        read_inventory(args.baseline, args.baseline_shards),
        read_inventory(args.candidate, args.candidate_shards),
    )
    if args.baseline_wall_seconds is not None or args.candidate_wall_seconds is not None:
        if not (args.baseline_wall_seconds and args.baseline_wall_seconds > 0
                and args.candidate_wall_seconds and args.candidate_wall_seconds > 0
                and math.isfinite(args.baseline_wall_seconds) and math.isfinite(args.candidate_wall_seconds)):
            parser.error('Both wall durations must be positive')
        comparison['wall_speedup'] = round(args.baseline_wall_seconds / args.candidate_wall_seconds, 3)
        comparison['wall_reduction_percent'] = round(
            100 * (1 - args.candidate_wall_seconds / args.baseline_wall_seconds), 2)
    print(json.dumps(comparison, indent=2))
    return 0 if comparison['inventory_parity_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
