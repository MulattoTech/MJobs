"""Capture authenticated exact-head GitHub gates and actual checkout identities."""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import re
import subprocess

REPO = 'MulattoTech/MJobs'
REQUIRED = {'CI / required', 'Integration / PostgreSQL and Redis',
            'Disposable Compose / mock end-to-end',
            'Dependencies / introduced vulnerabilities', 'Security / secret scan'}


def raw(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True,
                            encoding='utf-8', timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr[:500])
    return result.stdout


def gh(*args):
    return json.loads(raw(*args))


parser = argparse.ArgumentParser()
parser.add_argument('--pr', type=int, required=True)
parser.add_argument('--head', required=True)
parser.add_argument('--runs', nargs='+', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
pr = gh('api', f'repos/{REPO}/pulls/{args.pr}')
assert pr['head']['sha'] == args.head, 'PR head changed'
snapshot = gh('pr', 'view', str(args.pr), '--repo', REPO, '--json',
              'headRefOid,baseRefOid,state,mergeable,mergeStateStatus,statusCheckRollup')
checks = [{k: c.get(k) for k in ['name', 'status', 'conclusion', 'detailsUrl']}
          for c in snapshot['statusCheckRollup']]
passed = {c['name'] for c in checks
          if c['status'] == 'COMPLETED' and c['conclusion'] == 'SUCCESS'}
commits = {}
for label, sha in [('head', args.head), ('base', pr['base']['sha']),
                   ('proposed_merge', pr['merge_commit_sha'])]:
    c = gh('api', f'repos/{REPO}/git/commits/{sha}')
    commits[label] = {'sha': sha, 'tree': c['tree']['sha'],
                      'parents': [p['sha'] for p in c['parents']]}
runs = []
for run in args.runs:
    meta = gh('run', 'view', str(run), '--repo', REPO, '--json',
              'headSha,status,conclusion,url,jobs,createdAt,updatedAt,event')
    assert meta['headSha'] == args.head, 'Run belongs to a different head'
    row = {k: v for k, v in meta.items() if k != 'jobs'}
    row['run_id'] = run
    row['jobs'] = []
    for job in meta['jobs']:
        item = {k: job.get(k) for k in ['databaseId', 'name', 'status', 'conclusion', 'url']}
        item['steps'] = [{k: s.get(k) for k in ['name', 'status', 'conclusion']}
                         for s in job['steps']]
        if job['status'] == 'completed' and job['conclusion'] == 'success':
            log = raw('run', 'view', str(run), '--repo', REPO, '--job',
                      str(job['databaseId']), '--log')
            item['raw_log_sha256'] = hashlib.sha256(log.encode()).hexdigest()
            lines = log.splitlines()
            commits_seen = []
            excerpts = []
            for i, line in enumerate(lines):
                clean_line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
                clean_line = re.sub(r'\^\[\[[0-9;]*m', '', clean_line)
                if 'git log -1 --format=%H' in line:
                    for subsequent in lines[i+1:i+4]:
                        match = re.search(r'\b([0-9a-f]{40})\s*$', subsequent)
                        if match:
                            commits_seen.append(match.group(1))
                            break
                if re.search(r'\bTest Files\s+|\bTests\s+|\d+ passed.*in \d|All required fast gates passed|Every integration shard passed', clean_line):
                    excerpts.append(clean_line)
            item['actual_checkout_commits'] = sorted(set(commits_seen))
            item['result_excerpts'] = excerpts
        row['jobs'].append(item)
    runs.append(row)
report = {'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'repository': REPO, 'pr': args.pr, 'head': args.head,
          'pr_url': pr['html_url'], 'pr_state': pr['state'],
          'mergeable': pr['mergeable'], 'merge_state_status': snapshot['mergeStateStatus'],
          'commits': commits, 'checks': checks, 'required_checks': sorted(REQUIRED),
          'missing_required_success': sorted(REQUIRED-passed),
          'source_tree_equals_proposed_merge': commits['head']['tree'] == commits['proposed_merge']['tree'],
          'runs': runs}
report['all_required_passed'] = not report['missing_required_success']
args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps({k: v for k, v in report.items() if k not in ['checks', 'runs']}))
for run in runs:
    print(json.dumps({'run': run['run_id'], 'conclusion': run['conclusion'],
                      'jobs': [{k: j.get(k) for k in ['name', 'conclusion', 'actual_checkout_commits', 'result_excerpts']}
                               for j in run['jobs']]}))
