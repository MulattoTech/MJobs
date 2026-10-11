"""Read-only public workflow and curated-documentation checks."""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ['README.md', 'AGENTS.md', 'CODEX_START.md', 'CONTRIBUTING.md', 'SECURITY.md',
        'docs/INSTALLATION.md', 'docs/DEVELOPMENT.md', 'docs/ARCHITECTURE.md',
        'docs/maintainers/AUTOMATION.md', 'docs/maintainers/RELEASES.md',
        'docs/maintainers/PUBLIC_REPOSITORY_MODERNIZATION.md']
PIN = re.compile(r'^[\w.-]+/[\w./-]+@[a-f0-9]{40}$')
LINK = re.compile(r'!?\[[^\]\n]*\]\(([^)\n]+)\)')


def check_markdown(root: Path, paths: list[str]) -> list[str]:
    errors = []
    for name in paths:
        path = root / name
        if not path.is_file():
            errors.append(f'Missing documented entry point: {name}')
            continue
        text = re.sub(r'```.*?```', '', path.read_text('utf-8'), flags=re.S)
        for value in LINK.findall(text):
            value = value.split(' "', 1)[0].strip('<> ')
            parts = urlsplit(value)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (path.parent / unquote(parts.path)).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                errors.append(f'{name}: missing local link {value}')
    return errors


def check_workflows(root: Path) -> list[str]:
    errors = []
    for path in sorted((root / '.github/workflows').glob('*.yml')):
        doc = yaml.load(path.read_text('utf-8'), Loader=yaml.BaseLoader)
        if not isinstance(doc, dict):
            errors.append(f'{path.name}: invalid YAML')
            continue
        triggers = doc.get('on', {})
        if not isinstance(triggers, (str, list, dict)):
            errors.append(f'{path.name}: missing triggers')
            continue
        names = {triggers} if isinstance(triggers, str) else set(triggers)
        if 'pull_request_target' in names:
            errors.append(f'{path.name}: privileged PR event is forbidden')
        if doc.get('permissions') != {'contents': 'read'}:
            errors.append(f'{path.name}: default token permissions must be contents: read')
        for name, job in doc.get('jobs', {}).items():
            if 'uses' in job:
                continue
            if 'self-hosted' in str(job.get('runs-on', '')):
                errors.append(f'{path.name}/{name}: public CI must not use a personal runner')
            if not str(job.get('timeout-minutes', '')).isdigit():
                errors.append(f'{path.name}/{name}: explicit timeout required')
            if 'pull_request' in names and any(v == 'write' for v in job.get('permissions', {}).values()):
                errors.append(f'{path.name}/{name}: PR job must not have write permissions')
            for service in job.get('services', {}).values():
                if "--health-cmd '" in service.get('options', ''):
                    errors.append(f'{path.name}/{name}: service health commands require runner-compatible double quotes')
            for step in job.get('steps', []):
                action = step.get('uses')
                if action and not action.startswith('./') and not PIN.fullmatch(action):
                    errors.append(f'{path.name}/{name}: action is not SHA-pinned: {action}')
                if action and action.startswith('actions/checkout@'):
                    if step.get('with', {}).get('persist-credentials') != 'false':
                        errors.append(f'{path.name}/{name}: checkout must not persist credentials')
    return errors


def check_private_paths(root: Path) -> list[str]:
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True)
    errors = []
    for item in result.stdout.decode('utf-8').split('\0'):
        if not item:
            continue
        path = Path(item)
        if path.name == '.env' or item.startswith(('outputs/', '.worktrees/')) or path.suffix in {'.bundle', '.pfx', '.p12'}:
            errors.append(f'Private/runtime artifact is tracked: {item}')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    errors = check_markdown(root, DOCS) + check_workflows(root) + check_private_paths(root)
    for error in errors:
        print(error)
    print(f'Public repository policy: {"FAIL" if errors else "PASS"} ({len(errors)} errors)')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
