"""Idempotently mirror public MJobs issue labels and acceptance into GitHub Projects.

Uses the current gh login, never stores a personal token in repository secrets.
Read-only unless --apply is supplied. Does not close issues or alter acceptance.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
OWNER = 'MulattoTech'
REPO = 'MulattoTech/MJobs'


def gh(*args: str, data: dict | None = None) -> dict | list:
    command = ['gh', *args]
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data else None,
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr[-600:])
    value = json.loads(result.stdout) if result.stdout.strip() else {}
    if isinstance(value, dict) and value.get('errors'):
        raise RuntimeError(json.dumps(value['errors'])[:800])
    return value


def graphql(query: str) -> dict:
    return gh('api', 'graphql', data={'query': query})['data']


def batches(operations: list[str], size: int = 15):
    for start in range(0, len(operations), size):
        subset = operations[start:start + size]
        yield graphql('mutation { ' + ' '.join(f'op{i}:{op}' for i, op in enumerate(subset)) + ' }')
        time.sleep(1.3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-number', type=int, default=1)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    project = gh('project', 'view', str(args.project_number), '--owner', OWNER, '--format', 'json')
    project_id = project['id']
    fields = gh('project', 'field-list', str(args.project_number), '--owner', OWNER, '--format', 'json')['fields']
    field_by_name = {field['name']: field for field in fields}
    issues = []
    for page in range(1, 100):
        part = gh('api', f'repos/{REPO}/issues?state=all&per_page=100&page={page}')
        issues += [item for item in part if 'pull_request' not in item]
        if len(part) < 100:
            break
    items = gh('project', 'item-list', str(args.project_number), '--owner', OWNER,
               '--limit', '2000', '--format', 'json')['items']
    existing = {item['content']['number']: item['id'] for item in items
                if item.get('content', {}).get('repository') == REPO and item['content'].get('type') == 'Issue'}
    prior_values = {item['content']['number']: item for item in items
                    if item.get('content', {}).get('repository') == REPO and item['content'].get('type') == 'Issue'}
    missing = [issue for issue in issues if issue['number'] not in existing]
    print(json.dumps({'project': project['url'], 'issues': len(issues), 'existing': len(existing),
                      'to_add': len(missing), 'mode': 'apply' if args.apply else 'audit'}), flush=True)
    if not args.apply:
        return 0
    operations = [f'addProjectV2ItemById(input:{{projectId:{json.dumps(project_id)},contentId:{json.dumps(i["node_id"])}}}){{item{{id content{{... on Issue{{number}}}}}}}}' for i in missing]
    added = 0
    for result in batches(operations):
        for value in result.values():
            item = value['item'];existing[item['content']['number']] = item['id'];added += 1
        print(f'Project items added: {added}/{len(missing)}', flush=True)
    index = json.loads((ROOT/'planning/REQUIREMENT_INDEX.json').read_text('utf-8'))
    acceptance = {row['issue']: row['status'] for row in index['requirements']}
    updates = []
    for issue in issues:
        labels = [label['name'] for label in issue.get('labels', [])]
        values = {
            'Priority': next((s.split(':', 1)[1] for s in labels if s.startswith('priority:')), 'P3'),
            'Acceptance': acceptance.get(issue['number'], 'UNTRACKED'),
            'Area': next((s.split(':', 1)[1] for s in labels if s.startswith('area:')), 'untriaged'),
            'Category': next((s.split(':', 1)[1] for s in labels if s.startswith('category:')), 'other'),
        }
        for name, value in values.items():
            if prior_values.get(issue['number'], {}).get(name.lower()) == value:
                continue
            field = field_by_name.get(name)
            if not field:
                continue
            if field.get('options'):
                option = next((x['id'] for x in field['options'] if x['name'] == value), None)
                if not option:
                    continue
                field_value = '{singleSelectOptionId:' + json.dumps(option) + '}'
            else:
                field_value = '{text:' + json.dumps(value) + '}'
            updates.append('updateProjectV2ItemFieldValue(input:{projectId:' + json.dumps(project_id) +
                           ',itemId:' + json.dumps(existing[issue['number']]) + ',fieldId:' + json.dumps(field['id']) +
                           ',value:' + field_value + '}){projectV2Item{id}}')
    written = 0
    for result in batches(updates):
        written += len(result)
        if written % 150 == 0 or written == len(updates):
            print(f'Project metadata updated: {written}/{len(updates)}', flush=True)
    print(f'Completed: {len(issues)} public issues mirrored. No issue states or acceptance records changed.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
