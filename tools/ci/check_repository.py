"""Read-only public workflow and curated-documentation checks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shlex
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
                if 'workflow_run' in names:
                    if action and action.startswith('actions/download-artifact@'):
                        errors.append(f'{path.name}/{name}: privileged review must not consume PR artifacts')
                    if action and action.startswith('actions/checkout@') and step.get('with', {}).get('ref') != 'main':
                        errors.append(f'{path.name}/{name}: workflow_run checkout must explicitly use trusted main')
                if action and action.startswith('actions/checkout@'):
                    if step.get('with', {}).get('persist-credentials') != 'false':
                        errors.append(f'{path.name}/{name}: checkout must not persist credentials')
    return errors


def _unconditional_failure_gate(item: object) -> bool:
    return (isinstance(item, dict) and 'if' not in item
            and item.get('continue-on-error') in (None, False, 'false'))


def _job_commands(job: dict) -> tuple[str, ...]:
    if not _unconditional_failure_gate(job):
        return ()
    commands = []
    for step in job.get('steps', []):
        if (_unconditional_failure_gate(step)
                and isinstance(step.get('run'), str)):
            commands.extend(
                line.strip()
                for line in step['run'].splitlines()
                if line.strip() and not line.lstrip().startswith('#')
            )
    return tuple(commands)


def _workflow_commands(root: Path, name: str) -> tuple[tuple[str, ...], dict]:
    path = root / '.github/workflows' / name
    if not path.is_file():
        return (), {}
    doc = yaml.load(path.read_text('utf-8'), Loader=yaml.BaseLoader)
    if not isinstance(doc, dict):
        return (), {}
    commands = []
    for job in doc.get('jobs', {}).values():
        if isinstance(job, dict):
            commands.extend(_job_commands(job))
    return tuple(commands), doc


def _runs_command(commands: tuple[str, ...], marker: str) -> bool:
    for command in commands:
        if command != marker and not command.startswith(f'{marker} '):
            continue
        suffix = command[len(marker):]
        if not any(operator in suffix for operator in '|;&') \
                and not suffix.rstrip().endswith('\\'):
            return True
    return False


def _package_script_commands(script: object) -> tuple[str, ...]:
    if not isinstance(script, str):
        return ()
    lexer = shlex.shlex(script, posix=True, punctuation_chars='|&;')
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ()
    commands = []
    current = []
    for token in tokens:
        if token == '&&':
            if not current:
                return ()
            commands.append(' '.join(current))
            current = []
        elif token and set(token) <= set('|&;'):
            return ()
        else:
            current.append(token)
    if not current:
        return ()
    commands.append(' '.join(current))
    return tuple(commands)


def _has_workflow_trigger(doc: dict, trigger: str) -> bool:
    configured = doc.get('on')
    if isinstance(configured, str):
        return configured == trigger
    if isinstance(configured, (list, dict)):
        return trigger in configured
    return False


def check_required_ci(root: Path) -> list[str]:
    """Require every MJ-REQ-278 boundary in offline pull-request CI."""
    errors = []
    fast_commands, fast = _workflow_commands(root, 'ci.yml')
    mock_commands, mock = _workflow_commands(root, 'connected-acceptance.yml')
    for name, doc in (('ci.yml', fast), ('connected-acceptance.yml', mock)):
        if not _has_workflow_trigger(doc, 'pull_request'):
            errors.append(f'MJ-REQ-278: {name} must run on pull_request')
    required = {
        'backend lint': (fast_commands, 'python -m ruff check'),
        'backend type check': (fast_commands, 'python -m mypy'),
        'backend tests': (fast_commands, 'python -m pytest'),
        'frontend lint': (fast_commands, 'npm run lint --prefix apps/web'),
        'frontend type check': (fast_commands, 'npm run typecheck --prefix apps/web'),
        'frontend tests': (fast_commands, 'npm test --prefix apps/web'),
        'frontend build': (fast_commands, 'npm run build --prefix apps/web'),
        'mock E2E': (mock_commands, 'python -m cli.mock_e2e'),
    }
    for boundary, (commands, marker) in required.items():
        if not _runs_command(commands, marker):
            errors.append(f'MJ-REQ-278: missing enforced {boundary} boundary ({marker})')

    package_path = root / 'apps/web/package.json'
    try:
        scripts = json.loads(package_path.read_text('utf-8')).get('scripts', {})
    except (OSError, json.JSONDecodeError):
        scripts = {}
    script_markers = {
        'frontend lint': ('lint', 'node scripts/lint.mjs'),
        'frontend type check': ('typecheck', 'tsc --noEmit'),
        'frontend tests': ('test', 'vitest run'),
        'frontend build': ('build', 'vite build'),
    }
    for boundary, (name, marker) in script_markers.items():
        if not _runs_command(_package_script_commands(scripts.get(name)), marker):
            errors.append(f'MJ-REQ-278: web package does not define {boundary} script')

    mock_jobs = mock.get('jobs', {}) if isinstance(mock, dict) else {}
    fixture_mock_jobs = [job for job in mock_jobs.values() if isinstance(job, dict)
                         and _runs_command(_job_commands(job), 'python -m cli.mock_e2e')
                         and job.get('env', {}).get('MJOBS_OPERATION_MODE') == 'FIXTURE'
                         and job.get('env', {}).get('MJOBS_COMPOSE_MOCK') == '1']
    if not fixture_mock_jobs:
        errors.append('MJ-REQ-278: mock E2E must run in FIXTURE mode without external job websites')
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
    errors = (check_markdown(root, DOCS) + check_workflows(root)
              + check_required_ci(root) + check_private_paths(root))
    for error in errors:
        print(error)
    print(f'Public repository policy: {"FAIL" if errors else "PASS"} ({len(errors)} errors)')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
