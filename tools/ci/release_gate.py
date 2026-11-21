"""Read-only, fail-closed release/AFK completion gate. Reports never change acceptance."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = 'MulattoTech/MJobs'
WORKFLOWS = ('ci.yml', 'integration.yml', 'connected-acceptance.yml', 'browser-regression.yml', 'secret-scan.yml')


def unmet(rows: list[dict], scope: str) -> list[str]:
    if scope not in {'p0p1', 'all'}:
        raise ValueError('Unknown acceptance scope')
    selected = [row for row in rows if scope == 'all' or row.get('priority') in {'P0', 'P1'}]
    if not selected:
        return ['No acceptance criteria were selected']
    return [str(row.get('id', 'missing-id')) for row in selected if row.get('status') != 'PASS']


def acceptance_authority_errors(index: dict, capability: dict, reference: dict) -> list[str]:
    """Reject a derived index that could hide or promote canonical acceptance rows."""
    canonical: dict[str, dict] = {}
    capability_rows = capability.get('requirements', [])
    reference_ids = reference.get('canonical_ids', [])
    reference_results = reference.get('result_by_id', {})
    if len(capability_rows) != 366 or len(reference_ids) != 59:
        return ['Canonical acceptance matrix coverage is incomplete']
    for row in capability_rows:
        identifier = row.get('requirement_id')
        if not isinstance(identifier, str) or identifier in canonical:
            return ['Canonical acceptance matrix identifiers are missing or duplicated']
        canonical[identifier] = {'status': row.get('result'), 'priority': row.get('priority')}
    for identifier in reference_ids:
        if not isinstance(identifier, str) or identifier in canonical or identifier not in reference_results:
            return ['Canonical acceptance matrix identifiers are missing or duplicated']
        entry = reference_results[identifier]
        feature = reference.get('commercial_features', {}).get(identifier, {})
        canonical[identifier] = {
            'status': entry.get('result'),
            'priority': feature.get('priority', 'P3'),
        }
    index_rows = index.get('requirements', [])
    indexed: dict[str, dict] = {}
    for row in index_rows:
        identifier = row.get('id')
        if not isinstance(identifier, str) or identifier in indexed:
            return ['Requirement index identifiers are missing or duplicated']
        indexed[identifier] = row
    if set(indexed) != set(canonical):
        return ['Requirement index coverage differs from canonical acceptance matrices']
    errors = []
    for identifier, authority in canonical.items():
        row = indexed[identifier]
        if row.get('priority') != authority['priority']:
            errors.append(f'{identifier}: requirement index priority differs from canonical catalogue')
        if row.get('status') != authority['status']:
            errors.append(f'{identifier}: requirement index status differs from canonical acceptance matrix')
    return errors


def command(*args: str) -> str:
    result = subprocess.run(list(args), check=False, cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr[-500:])
    return result.stdout.strip()


def api(path: str) -> dict | list:
    return json.loads(command('gh', 'api', path))


def evaluate(scope: str, online: bool) -> dict:
    index = json.loads((ROOT / 'planning/REQUIREMENT_INDEX.json').read_text('utf-8'))
    capability = json.loads((ROOT / 'planning/CAPABILITY_MATRIX.json').read_text('utf-8'))
    reference_matrix = json.loads((ROOT / 'planning/REFERENCE_GAP_MATRIX.json').read_text('utf-8'))
    rows = index['requirements']
    original = [row for row in rows if row.get('id', '').startswith('MJ-REQ-')]
    reference_rows = [row for row in rows if row.get('id', '').startswith('MJ-REF-')]
    errors = []
    errors.extend(acceptance_authority_errors(index, capability, reference_matrix))
    if len(original) != 366 or len(reference_rows) != 59 or len({row['id'] for row in rows}) != 425:
        errors.append('Canonical 366 + 59 coverage is incomplete or duplicated')
    missing = unmet(rows, scope)
    if missing:
        errors.append(f'{len(missing)} selected acceptance requirements are not PASS')
    sha = command('git', 'rev-parse', 'HEAD')
    if command('git', 'status', '--porcelain=v1', '--untracked-files=all'):
        errors.append('Source checkout is not clean')
    checks = {}
    security = None
    if online:
        try:
            published = api(f'repos/{REPO}/commits/main')['sha']
            if published != sha:
                errors.append('Current HEAD is not the published main revision')
            for workflow in WORKFLOWS:
                result = api(f'repos/{REPO}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=30')
                runs = [run for run in result.get('workflow_runs', []) if run['head_sha'] == sha
                        and run['event'] in {'push', 'workflow_dispatch', 'schedule'}]
                latest = runs[0] if runs else None
                checks[workflow] = {'run': latest['id'], 'conclusion': latest['conclusion']} if latest else {'conclusion': 'NOT_RUN'}
                if not latest or latest['status'] != 'completed' or latest['conclusion'] != 'success':
                    errors.append(f'{workflow}: latest exact-main run is not successful')
            alerts = []
            for page in range(1, 100):
                part = api(f'repos/{REPO}/code-scanning/alerts?state=open&per_page=100&page={page}')
                alerts += part
                if len(part) < 100:
                    break
            security = sum(alert['rule'].get('security_severity_level') in {'high', 'critical'} for alert in alerts)
            if security:
                errors.append(f'{security} open high/critical CodeQL findings require review')
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            errors.append('Online verification unavailable: ' + str(error)[:350])
    else:
        errors.append('Exact-head hosted checks and security findings NOT_RUN; use --online')
    return {'ready': not errors, 'scope': scope, 'head': sha, 'unmet_count': len(missing),
            'unmet': missing, 'checks': checks, 'open_high_critical_findings': security, 'errors': errors}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope', choices=['p0p1', 'all'], default='all')
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--report', action='store_true', help='Report readiness without treating a blocked result as publication permission')
    args = parser.parse_args()
    result = evaluate(args.scope, args.online)
    print(json.dumps(result, indent=2))
    return 0 if args.report or result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
