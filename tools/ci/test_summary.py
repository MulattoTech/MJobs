"""Summarize JUnit counts without publishing test payloads or exception text."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import xml.etree.ElementTree as ET


def summarize(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
    return {key: sum(int(x.get(key, 0)) for x in suites)
            for key in ('tests', 'failures', 'errors', 'skipped')}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args()
    lines = ['## Executed test results',
             '| Report | Tests | Failures | Errors | Skipped |',
             '|---|---:|---:|---:|---:|']
    for report in args.reports:
        if not report.exists():
            lines.append(f'| {report.name} | NOT_RUN | | | |')
            continue
        values = summarize(report)
        lines.append('| ' + report.name + ' | ' + ' | '.join(str(v) for v in values.values()) + ' |')
    lines.append('\nSkipped and missing tests are not acceptance passes.')
    text = '\n'.join(lines) + '\n'
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(text)


if __name__ == '__main__':
    main()
