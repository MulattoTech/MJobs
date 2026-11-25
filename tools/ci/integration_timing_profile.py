"""Refresh committed integration scheduling weights from a complete successful run."""
from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def integration_files(root: Path) -> list[str]:
    folder = root / 'tests/integration'
    return sorted(p.relative_to(root).as_posix() for p in
                  set(folder.rglob('test_*.py')) | set(folder.rglob('*_test.py')))


def report_timings(report: Path, files: list[str]) -> dict[str, float]:
    modules = {'.'.join(Path(p).with_suffix('').parts): p for p in files}
    weights: dict[str, float] = {}
    for case in ET.parse(report).iter('testcase'):
        module = case.get('classname', '')
        while module and module not in modules:
            module = module.rpartition('.')[0]
        if not module:
            raise ValueError(f'Unmapped testcase in {report.name}: {case.get("classname")}')
        if case.find('failure') is not None or case.find('error') is not None:
            raise ValueError(f'Failed testcase in {report.name}')
        seconds = float(case.get('time', 'nan'))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f'Invalid testcase duration in {report.name}')
        path = modules[module]
        weights[path] = weights.get(path, 0.0) + seconds
    if not weights:
        raise ValueError(f'Empty test report: {report.name}')
    return weights


def complete_profile(files: list[str], reports: list[Path]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for report in reports:
        current = report_timings(report, files)
        if weights.keys() & current.keys():
            raise ValueError('A test file appeared in multiple shard reports')
        weights.update(current)
    missing = set(files) - weights.keys()
    if missing:
        raise ValueError(f'Missing test files: {sorted(missing)}')
    if not weights:
        raise ValueError('No test timing data')
    return {p: round(seconds, 3) for p, seconds in sorted(weights.items())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=int, required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('reports', type=Path, nargs='+')
    args = parser.parse_args()
    if args.run <= 0 or not re.fullmatch(r'[0-9a-f]{40}', args.head):
        parser.error('Expected a positive run ID and full lowercase commit SHA')
    weights = complete_profile(integration_files(ROOT), args.reports)
    args.output.write_text(json.dumps({'source_run': args.run, 'source_head': args.head,
                                       'seconds': weights}, indent=2) + '\n', encoding='utf-8')
    print(f'Profiled {len(weights)} files from {len(args.reports)} reports')


if __name__ == '__main__':
    main()
