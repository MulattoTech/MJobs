"""Verify disjoint unit shard manifests and combine JUnit evidence for existing consumers."""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def verify_manifests(manifests: list[dict], count: int) -> int:
    if count < 1 or len(manifests) != count or {m['index'] for m in manifests} != set(range(count)):
        raise ValueError('Missing or duplicate unit shard')
    inventory = manifests[0]['inventory']
    full = set(inventory)
    if not full or len(full) != len(inventory):
        raise ValueError('Invalid full unit inventory')
    seen: set[str] = set()
    for m in manifests:
        selected = set(m['selected'])
        if (m['count'] != count or m['inventory'] != inventory or not selected
                or len(selected) != len(m['selected']) or sorted(selected) != m['completed']
                or seen & selected or not selected <= full):
            raise ValueError('Unit collection or execution evidence mismatch')
        seen.update(selected)
    if seen != full:
        raise ValueError('Unit shards did not execute the entire inventory')
    return len(full)


def reconcile(directory: Path, count: int, output: Path) -> int:
    manifests = []
    suites = ET.Element('testsuites')
    for index in range(count):
        folder = directory / f'unit-shard-{index}'
        manifest = json.loads((folder / 'unit-collection.json').read_text(encoding='utf-8'))
        if manifest['index'] != index:
            raise ValueError('Shard report path/index mismatch')
        manifests.append(manifest)
        root = ET.parse(folder / 'unit.xml').getroot()
        reports = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
        cases = sum(len(list(report.iter('testcase'))) for report in reports)
        if (not reports or cases < len(manifest['selected'])
                or any(int(report.get(key, '0')) for report in reports for key in ('failures', 'errors'))):
            raise ValueError('Incomplete or failed unit JUnit evidence')
        suites.extend(reports)
    total = verify_manifests(manifests, count)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suites).write(output, encoding='utf-8', xml_declaration=True)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(f'Verified {reconcile(args.directory, args.count, args.output)} collected unit tests exactly once')


if __name__ == '__main__':
    main()
