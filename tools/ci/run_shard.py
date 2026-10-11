"""Stable test-file sharding; a failed or empty shard cannot report success."""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def select_files(root: Path, suite: str, index: int, count: int) -> list[str]:
    if suite not in {'browser', 'integration'} or count < 1 or not 0 <= index < count:
        raise ValueError('Invalid test shard')
    files = sorted((root / 'tests' / suite).glob('test_*.py'))
    return [path.relative_to(root).as_posix() for n, path in enumerate(files) if n % count == index]


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
