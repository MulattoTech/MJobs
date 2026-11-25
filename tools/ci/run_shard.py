"""Stable test-file sharding; a failed or empty shard cannot report success."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from statistics import median

from tools.ci.integration_split import invocation, target_weights
from tools.ci.integration_timing_profile import report_timings
from tools.ci.shard_evidence import PROOF_NAME, TEMPLATE_TARGET, verify_shard
from tools.ci.junit_identity import load_hosted_skips

ROOT = Path(__file__).resolve().parents[2]


def weighted_groups(files: list[str], count: int, timings: dict[str, float]) -> list[list[str]]:
    """Longest-processing-time scheduling with deterministic, nonempty groups."""
    known = {path: value for path in files
             if isinstance(value := timings.get(path), (int, float))
             and math.isfinite(value) and value > 0}
    fallback = median(known.values()) if known else 1.0
    groups: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for path in sorted(files, key=lambda path: (-known.get(path, fallback), path)):
        index = min(range(count), key=lambda i: (totals[i], len(groups[i]), i))
        groups[index].append(path)
        totals[index] += known.get(path, fallback)
    return [sorted(group) for group in groups]


def select_files(root: Path, suite: str, index: int, count: int) -> list[str]:
    if suite not in {'browser', 'integration'} or count < 1 or not 0 <= index < count:
        raise ValueError('Invalid test shard')
    folder = root / 'tests' / suite
    files = sorted(set(folder.rglob('test_*.py')) | set(folder.rglob('*_test.py')))
    paths = [path.relative_to(root).as_posix() for path in files]
    profile = root / f'tools/ci/{suite}_timings.json'
    if profile.is_file():
        data = json.loads(profile.read_text(encoding='utf-8'))
        targets, timings = paths, data['seconds']
        if suite == 'integration':
            targets, timings = target_weights(paths, timings, data.get('case_seconds', {}))
        return weighted_groups(targets, count, timings)[index]
    return [path for n, path in enumerate(paths) if n % count == index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['browser', 'integration'], required=True)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--hosted', action='store_true', help='Enforce reviewed hosted skip contract')
    args = parser.parse_args()
    selected = select_files(ROOT, args.suite, args.index, args.count)
    if not selected:
        raise ValueError('Empty test shard must not pass')
    print(f'{args.suite} shard {args.index + 1}/{args.count}: {len(selected)} targets', flush=True)
    if args.list:
        print('\n'.join(selected))
        return 0
    output = ROOT / 'outputs/ci'
    output.mkdir(parents=True, exist_ok=True)
    report = output / f'{args.suite}-{args.index}.xml'
    report.unlink(missing_ok=True)
    manifest_path = output / f'{args.suite}-{args.index}-collection.json'
    manifest_path.unlink(missing_ok=True)
    split = args.suite == 'integration' and (ROOT / 'tools/ci/integration_timings.json').is_file()
    paths, excluded = invocation(selected) if split else (selected, [])
    command = [sys.executable, '-m', 'pytest', '-q', *paths, '--junitxml', str(report)]
    env = os.environ.copy()
    env['MJOBS_CI_SHARD_COLLECTION_PATH'] = str(manifest_path)
    env['MJOBS_CI_EXCLUDE_NODES'] = json.dumps(excluded)
    command.extend(['-p', 'tools.ci.shard_collection'])
    postgres_template = (args.suite == 'integration'
                         and env.get('MJOBS_CI_POSTGRES_TEMPLATE') == '1')
    if TEMPLATE_TARGET in selected:
        (output / PROOF_NAME).unlink(missing_ok=True)
        if postgres_template:
            from fixtures.postgres_ci_template import require_template_inputs
            require_template_inputs(env)
    if args.suite == 'browser':
        command.extend(['--durations=20', '--durations-min=0.5'])
    if args.suite == 'integration':
        command.extend(['-p', 'tools.ci.shard_selection', '--durations=20', '--durations-min=0.5'])
    result = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    if result == 0:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        verify_shard(manifest, report, selected, split=split,
                     postgres_template=postgres_template,
                     allowed_skips=load_hosted_skips(ROOT, args.suite) if args.hosted else None)
        manifest.update(suite=args.suite, index=args.index, count=args.count, targets=selected)
        manifest_path.write_text(json.dumps(manifest) + '\n', encoding='utf-8')
        observed = report_timings(report, selected, split=split)
        if set(observed) != set(selected):
            raise ValueError(f'Missing integration reports for: {sorted(set(selected) - observed.keys())}')
        print(f'Verified execution reports for all {len(selected)} assigned targets', flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
