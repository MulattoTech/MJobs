"""Record reviewed P1 acceptance only after protected source integration."""
from pathlib import Path
from collections import Counter
import argparse
import datetime
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
SOURCE = 'ca1e62c6dc64ef4149bba577f02973684e259e16'
IDS = {f'MJ-REQ-{n:03}' for n in [20,21,30,34,157,159,163,164,170,
    213,214,215,216,217,219,220,222,223,225,226,227,228,229,230,
    255,256,258,259,260,261,262,271,274,278,279,280,281,282,283,284,285,286,287]}
RUNS = [41278248822, 41278248903, 41278248825]
EVIDENCE = 'docs/evidence/2026-11-01-pro-p1-delivery.json'
DIRECTORY = 'docs/evidence/2026-11-01-pro-p1'


def read(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True, type=Path)
parser.add_argument('--source-merge', required=True)
args = parser.parse_args()
repo = args.repo.resolve()
if not __debug__:
    raise RuntimeError('Recording requires enabled verification assertions')


def git(*arguments):
    return subprocess.check_output(['git', *arguments], cwd=repo, text=True).strip()


assert git('rev-parse','HEAD') == args.source_merge, 'Recording must start at integrated source'
assert not git('diff','--name-only') and not git('diff','--cached','--name-only'), 'Tracked edits present'
gates = read('p1-source-gates.json')
assert gates['head'] == SOURCE and gates['all_required_passed']
required = {'CI / required', 'Integration / PostgreSQL and Redis',
            'Disposable Compose / mock end-to-end',
            'Dependencies / introduced vulnerabilities', 'Security / secret scan'}
assert gates['repository']=='MulattoTech/MJobs' and gates['pr']==875
assert set(gates['required_checks'])==required and not gates['missing_required_success']
passed={c['name'] for c in gates['checks']
        if c['status']=='COMPLETED' and c['conclusion']=='SUCCESS'}
assert required <= passed
assert {r['run_id'] for r in gates['runs']}==set(RUNS) and len(gates['runs'])==len(RUNS)
assert gates['source_tree_equals_proposed_merge']
assert git('rev-parse','HEAD^{tree}') == gates['commits']['head']['tree']
assert gates['commits']['head']['sha']==SOURCE
assert git('rev-parse',SOURCE+'^{tree}')==gates['commits']['head']['tree']
assert gates['commits']['proposed_merge']['tree']==gates['commits']['head']['tree']
assert all(r['conclusion']=='success' and r['status']=='completed' for r in gates['runs'])
expected_merge = gates['commits']['proposed_merge']['sha']
for run in gates['runs']:
    assert run['headSha']==SOURCE and run['jobs']
    for job in run['jobs']:
        assert job['status']=='completed' and job['conclusion']=='success'
        observed=job.get('actual_checkout_commits',[])
        if job['name'] not in {'CI / required','Integration / PostgreSQL and Redis'}:
            assert observed,job['name']
        assert all(c==expected_merge for c in observed)
merge = read('p1-source-merge.json')
assert merge['merged'] is True and merge['sha'] == args.source_merge
audits = [read(f'hosted-{n}.json') for n in RUNS]
assert [a['run_id'] for a in audits]==RUNS
assert all(a['headSha']==SOURCE and a['status']=='completed' and a['conclusion']=='success' for a in audits)
browser = read('remote-pro-p1-ca1e-browser-evidence.json')
assert browser['revision']==SOURCE and browser['exit_code']==0
assert browser['source_tree']==git('rev-parse',SOURCE+'^{tree}')
assert browser['external_submission_enabled'] is False
assert browser['cases'] and browser['suite_totals']['tests']>0
assert browser['suite_totals']['failures']==browser['suite_totals']['errors']==browser['suite_totals']['skipped']==0
for path,digest in browser['source_test_hashes'].items():
    assert sha(repo/path)==digest,path
assert all(c['result']=='passed' for c in browser['cases'])
live = read('remote-harness-live-persistence-evidence.json')
assert live['revision']=='e69638121e79a40632afdef80b70befa1de4ecfa'
assert live['script_sha256']==sha(ROOT/'p1-live-persistence-b22ea2de.py')
assert {s['provider'] for s in live['sources'] if s['result']=='PASS'} >= {'remotive','remoteok'}
greenhouse = read('remote-harness-greenhouse-live-evidence.json')
assert greenhouse['revision']==SOURCE and greenhouse['source']['result']=='PASS'
assert greenhouse['script_sha256']==sha(ROOT/'harness_greenhouse_persistence_probe.py')
assert greenhouse['helper_sha256']==sha(ROOT/'harness_live_persistence_probe.py')
assert live['company']['result']=='PASS'
review = read('p1-reviewed-draft.json')
by_id = {r['requirement_id']:r for r in review['requirements']}
assert review['source_head_at_assembly']==SOURCE
assert len(review['requirements'])==len(by_id)==44 and len(IDS)==43
assert set(by_id)==IDS|{'MJ-REQ-254'}
assert {i for i,r in by_id.items() if r['candidate_for_new_pass']}==IDS
assert all(by_id[i]['proposed_result_after_required_gates']=='PASS'
           and not by_id[i]['current_remaining_gaps'] for i in IDS)
assert by_id['MJ-REQ-254']['candidate_for_new_pass'] is False
assert by_id['MJ-REQ-254']['proposed_result']=='PARTIAL'
assert not review['missing_reviews']
for path,digest in review['source_file_sha256'].items():
    assert sha(repo/path)==digest,path
for path,digest in review['evidence_file_sha256'].items():
    assert sha(Path(path))==digest,path
for record in review['review_input_manifest'].values():
    assert sha(Path(record['path']))==record['sha256'],record['path']
for bridge in review['live_source_identity_bridge']:
    assert bridge['assembly_revision']==SOURCE
    for revision in bridge['same_as_recorded_revisions']:
        assert git('rev-parse',revision+':'+bridge['path'])==bridge['git_object']
execution_review=read('acceptance-review-executions.json')
assert execution_review['status']=='EXECUTION_RECONCILIATION_COMPLETE'
assert execution_review['required_coverage_result']=='PASS'
assert execution_review['expected_source_head']==SOURCE
for key in ('missing_imports','evidence_errors','unresolved','named_node_problems',
            'required_named_node_issues','required_module_issues','required_postgres_rq_issues','failed_hosted_gates'):
    assert not execution_review[key],key
for record in execution_review['inputs']+execution_review['artifacts']:
    assert sha(Path(record['path']))==record['sha256'],record['path']
required_named=[n for n in execution_review['named_nodes'] if set(n['requirements'])&IDS]
assert required_named and all(n['status']=='PASS' and n['source_node_exists'] for n in required_named)
pg_rq=[n for group in execution_review['required_postgres_and_rq_cases'] for n in group['nodes']]
assert len(pg_rq)==17
assert all(n['status']=='PASS' and n['executions']
           and all(e['result']=='passed' for e in n['executions']) for n in pg_rq)
stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
matrix_path = repo/'planning/CAPABILITY_MATRIX.json'
matrix = json.loads(matrix_path.read_text())
reference_path = repo/'planning/REFERENCE_GAP_MATRIX.json'
reference_hash = sha(reference_path)
before = {r['requirement_id']:dict(r) for r in matrix['requirements']}
assert len(before)==366
assert sum(r['result']=='PASS' for r in before.values() if r['priority']=='P1')==49
assert all(before[i]['priority']=='P1' and before[i]['result']!='PASS' for i in IDS)
assert all(before[i]['priority']=='P1' and before[i]['result']==by_id[i]['before'] for i in IDS|{'MJ-REQ-254'})

artifacts = {}
def copy(name, target):
    source = ROOT/name
    destination = repo/DIRECTORY/target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source,destination)
    artifacts[f'{DIRECTORY}/{target}'] = sha(destination)

review_names = ['acceptance-review-data-search.json','acceptance-review-docs.json',
    'acceptance-review-harness.json','acceptance-review-operations.json',
    'acceptance-review-operations-2.json','acceptance-review-final-claims.json',
    'acceptance-review-ui-deadline-gaps.json','acceptance-review-live.json',
    'acceptance-review-final-selector.json','acceptance-review-greenhouse-probe.json',
    'acceptance-review-executions.json']
for name in review_names:
    copy(name, 'reviews/'+name)
for name in ['remote-harness-live-read-evidence.json','remote-harness-live-persistence-evidence.json',
             'remote-harness-wwr-live-followup.json','remote-harness-greenhouse-live-evidence.json']:
    copy(name, 'live/'+name.removeprefix('remote-'))
for name in ['harness_live_read_probe.py','harness_live_persistence_probe.py',
             'harness_greenhouse_persistence_probe.py','p1-live-persistence-b22ea2de.py']:
    copy(name, 'repro/'+name)
copy('p1-source-gates.json','source-gates.json')
copy('p1-source-merge.json','source-merge.json')
copy('remote-pro-p1-ca1e-browser-evidence.json','windows-browser.json')
copy('p1-issues-before.json','issues-before.json')

criteria = {'recorded_at':stamp,'accepted_source':SOURCE,'integrated_source':args.source_merge,
            'source_tree':gates['commits']['head']['tree'],
            'requirements':[], 'source_file_sha256':review['source_file_sha256'],
            'live_source_identity_bridge':review['live_source_identity_bridge']}
for identifier in sorted(IDS|{'MJ-REQ-254'}):
    row = by_id[identifier]
    row['result'] = 'PASS' if identifier in IDS else 'PARTIAL'
    row.pop('proposed_result',None)
    row['acceptance_summary'] = row.pop('acceptance_summary_draft')
    criteria['requirements'].append(row)
dump(repo/DIRECTORY/'criteria.json',criteria)
artifacts[f'{DIRECTORY}/criteria.json']=sha(repo/DIRECTORY/'criteria.json')
selected_modules = {p[:-3].replace('/','.') for r in criteria['requirements']
                    for p in r['test_paths'] if p.endswith('.py')}
execution=[]
for audit in audits:
    item={k:v for k,v in audit.items() if k not in ['junit','synthetic_evidence']}
    item['junit']=[]
    for suite in audit['junit']:
        selected=[c for c in suite['cases'] if any(c['class']==m or c['class'].startswith(m+'.')
                                                  for m in selected_modules)]
        item['junit'].append({k:v for k,v in suite.items() if k!='cases'} |
                            {'selected_cases':selected,'selected_case_counts':dict(Counter(c['result'] for c in selected))})
    item['synthetic_evidence']=audit.get('synthetic_evidence',[])
    execution.append(item)
dump(repo/DIRECTORY/'hosted-execution.json',execution)
artifacts[f'{DIRECTORY}/hosted-execution.json']=sha(repo/DIRECTORY/'hosted-execution.json')

for row in matrix['requirements']:
    identifier=row['requirement_id']
    if identifier not in IDS|{'MJ-REQ-254'}:
        continue
    row['result']='PASS' if identifier in IDS else 'PARTIAL'
    row['evidence']=list(dict.fromkeys([*row['evidence'],EVIDENCE,f'{DIRECTORY}/criteria.json']))
    row['note']=row.get('note','')+' 2026-11-01 verified checkpoint: '+by_id[identifier]['acceptance_summary']
matrix['timestamp']=stamp
matrix['overall_result']='PARTIAL'
matrix['delivery_policy_note']='2026-11-01 user-authorized protected GitHub-hosted delivery: source PR875 passed exact-head required checks and was merged; this separately reviewed acceptance record promotes 43 original P1 requirements with criterion-specific executed evidence. Whole product remains PARTIAL; external submissions OFF. Historical policy checkpoints remain in version history.'
counts=lambda rows,priority:dict(Counter(r['result'] for r in rows if r['priority']==priority))
assert len(matrix['requirements'])==len(before)==366
assert sum(r['priority']=='P1' for r in matrix['requirements'])==116
assert counts(matrix['requirements'],'P1')['PASS']==92
assert sha(reference_path)==reference_hash
reference=json.loads(reference_path.read_text())
assert reference['reference_gap_count']==59
assert len(reference['canonical_ids'])==len(reference['result_by_id'])==59
assert all(row==before[row['requirement_id']] for row in matrix['requirements']
           if row['requirement_id'] not in IDS|{'MJ-REQ-254'})
dump(matrix_path,matrix)
summary={'schema_version':1,'recorded_at':stamp,
    'scope':'User-authorized P1 verified delivery; original requirements only',
    'overall_result':'PARTIAL','whole_product_release_readiness':'UNKNOWN',
    'external_submission_enabled':False,'paid_ai_calls':0,'emergency_cutoff_percent':7,
    'source':{'head':SOURCE,'proposed_merge':expected_merge,'merged_commit':args.source_merge,
              'git_tree':gates['commits']['head']['tree'],'pr':'https://github.com/MulattoTech/MJobs/pull/875'},
    'acceptance_record_integration':'This record requires its own protected PR; code integration above is already complete.',
    'original_denominator':366,'reference_denominator':59,'p1_denominator':116,
    'p1_before':counts(before.values(),'P1'),'p1_after':counts(matrix['requirements'],'P1'),
    'new_p1_pass_count':43,'newly_accepted':[{'id':i,'title':by_id[i]['title'],'previous':before[i]['result']} for i in sorted(IDS)],
    'partial_evidence_only':['MJ-REQ-254'],'p0_unchanged':counts(matrix['requirements'],'P0'),
    'remaining_p1':[{'id':r['requirement_id'],'result':r['result']} for r in matrix['requirements']
                    if r['priority']=='P1' and r['result']!='PASS'],
    'criteria_path':f'{DIRECTORY}/criteria.json','execution_path':f'{DIRECTORY}/hosted-execution.json',
    'artifact_sha256':artifacts,
    'known_limitations':[
        'MJ-REQ-254 remains PARTIAL: 21 active UI modules contain 32 unbounded injected-fetch dispatch sites.',
        'WWR live batch FAILS SOURCE_DUPLICATE_JOB_ID for conflicting content under one native identity. Both failed attempts are retained; 228 is supported by Remotive, RemoteOK and Greenhouse.',
        'Live source replay proves same-capture dedupe and persistence in isolated SQLite; it does not claim coincidental cross-provider jobs or external employer submissions.',
        'Windows, Linux and hosted executions have distinct scopes. Skips are recorded explicitly and never counted as passes.',
        'The onboarding synthetic artifact remains PARTIAL for its real-provider clause; all performed synthetic actions passed. Live provider requirements have separate explicit evidence.',
        'Original366 and additive59 acceptance authorities remain separate and retain their denominators.',
        'Production deployment and post-merge issue readback are verified separately after this protected acceptance PR.']}
dump(repo/EVIDENCE,summary)
entry={'milestone_id':'PRO-P1-VERIFIED-DELIVERY-20261101','timestamp':stamp,
    'current_phase':'P1_ACCEPTANCE_RECORDED',
    'requirements':sorted(IDS),'validated_product_commit':SOURCE,
    'completed_steps':['Protected source PR875 integrated after exact proposed-merge required gates.',
       'Recorded43 independently reviewed original P1 acceptances and bounded partial254 evidence; denominator unchanged.'],
    'files_added':[EVIDENCE,DIRECTORY+'/criteria.json',DIRECTORY+'/hosted-execution.json'],
    'files_modified':['planning/CAPABILITY_MATRIX.json','planning/REQUIREMENT_INDEX.json',
       'planning/IMPLEMENTATION_STATE.json','planning/SESSION_HANDOFF.md','docs/PROGRESS_DASHBOARD.md',
       'docs/PROGRESS_CARD.json','docs/PROGRESS_CARD.svg'],
    'tests_completed':['Final source required CI, all PostgreSQL/Redis shards and disposable Compose browser/recovery/backup PASS; exact JUnit outcomes and checkout revisions retained.',
       'Final source frontend1000PASS; exact-revision Windows critical browser manifest PASS.',
       'Three public sources captured and production-collected twice with two database reopens each; official employer careers/ATS/tenant discovery PASS.'],
    'tests_remaining':['Protected acceptance-record PR gates and final main deployment readback.'],
    'migrations_applied':[],
    'live_integrations_tested':['Remotive13jobs','RemoteOK99jobs','Greenhouse19jobs','Official Greenhouse careers page and exact ATS tenant'],
    'known_errors':['WWR conflicting native identity: SOURCE_DUPLICATE_JOB_ID; captured failed batch retained.'],
    'known_limitations':summary['known_limitations'],
    'last_safe_checkpoint':{'git_commit':args.source_merge,'evidence_path':EVIDENCE}}
dump(ROOT/'p1-milestone-entry.json',entry)
state_path=repo/'planning/IMPLEMENTATION_STATE.json'
state=json.loads(state_path.read_text())
state.update(current_milestone=entry['milestone_id'],current_objective='Integrate reviewed92/116 P1 acceptance and verify protected main deployment',
             generated_at=stamp,status='PARTIAL',release_readiness='UNKNOWN')
dump(state_path,state)
print(json.dumps({'evidence':EVIDENCE,'new_p1':43,'final_p1':92,'reference_matrix_unchanged':True,
                  'next':'Record milestone; reconcile index; refresh dashboard; validate; protected acceptance PR'}))
