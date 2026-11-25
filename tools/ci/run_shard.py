"""Stable test-file sharding; a failed or empty shard cannot report success."""
from __future__ import annotations
import argparse
import json
import math
from statistics import median
from pathlib import Path
import subprocess
import sys

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
    profile = root / 'tools/ci/integration_timings.json'
    if suite == 'integration' and profile.is_file():
        timings = json.loads(profile.read_text(encoding='utf-8'))['seconds']
        return weighted_groups(paths, count, timings)[index]
    return [path for n, path in enumerate(paths) if n % count == index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['browser', 'integration'], required=True)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    selected = select_files(ROOT, args.suite, args.index, args.count)
    if not selected:
        raise ValueError('Empty test shard must not pass')
    print(f'{args.suite} shard {args.index + 1}/{args.count}: {len(selected)} files', flush=True)
    if args.list:
        print('\n'.join(selected))
        return 0
    output = ROOT / 'outputs/ci'
    output.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', *selected, '--junitxml',
                           str(output / f'{args.suite}-{args.index}.xml')], cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())
