"""Execute the complete launcher flow against a local Git fixture and stubbed services.
No real GitHub requests, Codex models, credentials, or product acceptance changes.
"""
from pathlib import Path
import base64
import json
import os
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix='mjobs-round3-flow-'))
repo = ROOT / 'MJobs'
repo.mkdir()
(repo / 'tools' / 'orchestration').mkdir(parents=True)

def run(args, **kwargs):
    return subprocess.run(args, text=True, encoding='utf-8', errors='replace', capture_output=True, timeout=90, **kwargs)

def git(*args):
    result = run(['git', '-C', str(repo), *args])
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()

git('init', '--quiet', '--initial-branch=main')
(repo/'README.txt').write_text('Synthetic launcher integration fixture.\n',encoding='utf-8')
# Supply the exact production prompt/authority files in the fixture; a newly
# cloned campaign MUST fail if its canonical references were removed.
asset_files = [
    'AGENTS.md', '.agents/skills/mjobs-delivery/SKILL.md',
    'planning/CAPABILITY_MATRIX.json', 'planning/REFERENCE_GAP_MATRIX.json',
    'planning/REQUIREMENT_INDEX.json', 'planning/ISSUE_SEED.jsonl',
    'planning/SESSION_HANDOFF.md',
    'prompts/codex/project-board-end-to-end.md',
    'prompts/codex/board-p0p1-plan.md',
    'prompts/codex/board-p0p1-goal.md',
    'prompts/codex/board-p0p1-main.md',
    'prompts/codex/next-plan.md',
    'prompts/codex/next-goal.md',
    'prompts/codex/next-main.md',
]
for asset in asset_files:
    source = SOURCE / asset
    if not source.is_file():
        raise RuntimeError(f'Required test source is missing: {asset}')
    target = repo / asset
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        source.read_text(encoding='utf-8').replace(r'C:\Tools\Dev\MJobs', str(repo)),
        encoding='utf-8',
    )
(repo/'tools/orchestration/prompt_inventory.py').write_text(
    (SOURCE/'tools/orchestration/prompt_inventory.py').read_text(encoding='utf-8'),
    encoding='utf-8',
)
git('add', '-A')
git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--quiet','-m','Fixture seed')
head = git('rev-parse','HEAD')
module = (SOURCE/'tools/orchestration/MJobsRound3.psm1').read_text(encoding='utf-8')
board = {'totalCount':3, 'items':[{'id':f'card-{n}', 'content':{'type':'Issue','repository':'MulattoTech/MJobs','number':n},'priority':p,'labels':[f'priority:{p}'],'status':'Todo','acceptance':'PARTIAL','title':f'Fixture {n}','area':'fixture','category':'feature'} for n,p in [(1,'P0'),(2,'P1'),(3,'P2')]]}
matrix = {'requirements':[{'requirement_id':'MJ-REQ-000','priority':'P0','result':'PARTIAL'}, {'requirement_id':'MJ-REQ-001','priority':'P1','result':'PARTIAL'}]}
mock = r'''
$script:RealNative=${function:Invoke-MJobsProcess}
function Invoke-MJobsProcess {
 param($Program,$Arguments,$TimeoutSeconds=90,$WorkingDirectory='')
 if($Program -ne 'gh'){ return & $script:RealNative -Program $Program -Arguments $Arguments -TimeoutSeconds $TimeoutSeconds -WorkingDirectory $WorkingDirectory }
 $a=$Arguments -join ' '
 [IO.File]::AppendAllText($env:MJOBS_TEST_GH_LOG,$a+"`n")
 $stdout='';$stderr='';$code=0
 if($a -eq 'api --hostname github.com user'){
   if($env:MJOBS_TEST_FAIL_AUTH){$stdout='{"status":"401"}';$stderr='HTTP 401 Requires authentication';$code=1}
   else{$stdout='{"login":"MulattoTech"}'}
 }elseif($a -match ' graphql '){$stdout='{"data":{"user":{"projectV2":{"id":"PVT_FIXTURE","title":"Fixture","number":1,"closed":false,"items":{"totalCount":3}}}}}'}
 elseif($a -match '^project item-list '){$stdout='BOARD_JSON'}
 elseif($a -match '^pr list '){$stdout='[]'}
 elseif($a -match '/commits/main$'){$stdout='HEAD_JSON'}
 elseif($a -match '/contents/planning/CAPABILITY_MATRIX.json'){$stdout='MATRIX_JSON'}
 else{throw "Unexpected mocked gh command $a"}
 return @{ExitCode=$code;StdOut=$stdout;StdErr=$stderr}
}
'''.replace('BOARD_JSON',json.dumps(board,separators=(',',':'))).replace('HEAD_JSON',json.dumps({'sha':head})).replace('MATRIX_JSON',json.dumps({'encoding':'base64','content':base64.b64encode(json.dumps(matrix).encode()).decode()}))
module=module.replace('Export-ModuleMember -Function',mock+'\nExport-ModuleMember -Function')
(repo/'tools/orchestration/MJobsRound3.psm1').write_text(module,encoding='utf-8')
launcher=(SOURCE/'Start-MJobs-P0P1-Round3.ps1').read_text(encoding='utf-8')
# Test-copy substitution only: native git clone now uses the small local fixture.
launcher=launcher.replace('https://github.com/MulattoTech/MJobs.git',repo.as_posix())
(repo/'Start-MJobs-P0P1-Round3.ps1').write_text(launcher,encoding='utf-8')
bin_dir=ROOT/'bin';bin_dir.mkdir()
(bin_dir/'codex.ps1').write_text(r'''
$ErrorActionPreference='Stop'
[IO.File]::AppendAllText($env:MJOBS_TEST_CODEX_LOG,(ConvertTo-Json -InputObject @($args) -Compress)+"`n")
if($args[0] -eq 'exec'){
 $i=[Array]::IndexOf($args,'--output-last-message')
 if($i -lt 0){throw 'Missing result path'}
 if($args -contains '-s'){throw 'Conflicting sandbox flag remains'}
 if($args -notcontains '--approve-for-me'){throw 'Review option missing'}
 $result=@{status='CONTINUE';summary='SYNTHETIC FIXTURE ONLY';issueIds=@(1);prs=@();completeOriginalRequirements=@();nextAction='fixture'}
 [IO.File]::WriteAllText($args[$i+1],($result|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
 '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":20,"reasoning_output_tokens":10}}'
}else{ 'SYNTHETIC INTERACTIVE TUI EXIT' }
exit 0
''',encoding='utf-8')
env=os.environ.copy();env['PATH']=str(bin_dir)+os.pathsep+env['PATH'];env['MJOBS_TEST_GH_LOG']=str(ROOT/'gh-commands.txt');env['MJOBS_TEST_CODEX_LOG']=str(ROOT/'codex-commands.jsonl')
for key in ('GH_HOST','MJOBS_TEST_FAIL_AUTH'):
    env.pop(key,None)
passed=[]

def launch(*args, fail=None):
    result=run(['pwsh','-NoProfile','-File',str(repo/'Start-MJobs-P0P1-Round3.ps1'),*args],env=env,cwd=repo)
    (ROOT/f'case-{len(passed)+1}.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if fail:
        assert result.returncode != 0 and fail in result.stdout+result.stderr, result.stdout+result.stderr
    else:
        assert result.returncode == 0, result.stdout+result.stderr
    return result

launch('-Preview'); assert not list(ROOT.glob('MJobs-Round3-*'));passed.append('preview uses full mocked auth/Project flow without cloning')
launch('-PrepareOnly'); latest=repo/'.git/mjobs-p0p1-round3/latest.json';state=json.loads(latest.read_text(encoding='utf-8-sig'));clone=Path(state['worktree']);assert (clone/'.git').is_dir();assert Path(state['runDir']).is_relative_to(clone);passed.append('prepare clones real local Git and stores context inside its workspace')
assert not (ROOT/'codex-commands.jsonl').exists();passed.append('preparation never invokes a model')
launch('-Iterations','1'); state=json.loads(latest.read_text(encoding='utf-8-sig'));assert state['status']=='CONTINUE';calls=(ROOT/'codex-commands.jsonl').read_text().splitlines();assert len(calls)==1;passed.append('automatic iteration executes once and saves validated result')
directive_text=next(Path(state['runDir']).glob('directive-*.md')).read_text(encoding='utf8')
assert '## GOAL FROM EXISTING REVIEWED FILE' in directive_text
assert '## PLAN FROM EXISTING REVIEWED FILE' in directive_text
assert '## MAIN EXECUTION PROMPT FROM EXISTING REVIEWED FILE' in directive_text
assert 'MJ-REQ-276' in directive_text
assert 'PRIORITY #1 (RECONCILE, NOT REBUILD)' not in directive_text
passed.append('effective native Codex instruction uses current reviewed next-goal/plan/main, not a stale Round3 directive')

q=json.loads(next(Path(state['runDir']).glob('queue-*.json')).read_text());assert [x['issue'] for x in q]==[1,2];passed.append('staged dispatch queue excludes P2 in the full launcher')
launch('-Interactive');state=json.loads(latest.read_text(encoding='utf-8-sig'));assert state['status']=='INTERACTIVE_EXIT';calls=(ROOT/'codex-commands.jsonl').read_text().splitlines();assert len(calls)==2;args=json.loads(calls[-1]);assert args[0]!='exec' and '--json' not in args;passed.append('native interactive path inherits terminal and never starts another iteration')
launch('-Status');assert len((ROOT/'codex-commands.jsonl').read_text().splitlines())==2;passed.append('status never invokes a model')
count=len(list(ROOT.glob('MJobs-Round3-*')));env['MJOBS_TEST_FAIL_AUTH']='1';launch('-NewCampaign',fail='MJOBS_AUTH_REQUIRED');assert len(list(ROOT.glob('MJobs-Round3-*')))==count;assert len((ROOT/'codex-commands.jsonl').read_text().splitlines())==2;passed.append('invalid login blocks both clone creation and model dispatch')
receipt={'passed':len(passed),'failed':0,'checks':passed,'fixture_root':str(ROOT),'real_github_calls':0,'real_model_calls':0,'note':'Full launcher control-flow tests; not live product or account acceptance.'}
(ROOT/'verification.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2))
