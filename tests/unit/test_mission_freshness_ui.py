"""Execute Mission Control's UI against changing observer responses."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),requests=[];
const get=key=>{if(!elements.has(key)) elements.set(key,{
 textContent:'',innerHTML:'',className:'',handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},
 classList:{toggle(){}},querySelector:get
});return elements.get(key);};
let unavailable=false,incomplete=false;
const now=new Date().toISOString();
const progress={available:true,observed_at:now,source_root:'C:/fixture/MJobs',
 original:{pass:183,partial:82,not_run:101,total:366,accepted_percent:50},
 reference:{pass:19,partial:4,not_run:36,total:59,accepted_percent:32.2},
 git:{checkout_head_sha:'a'.repeat(40),checkout_branch:'main'},areas:[],priorities:[]};
const activity={available:true,read_only:true,observed_at:now,observation:'RECENT_ACTIVITY',
 latest_activity_utc:now,coverage:{partial:false,notes:[]},
 workspaces:[{workspace:'C:/fixture/current-worker',branch:'fix/current',head:'b'.repeat(40),
 changed_count:137,changed_files:['updated.py']},
 {workspace:'C:/fixture/unavailable-worker',branch:'fix/unavailable',head:null,
 changed_count:null,changed_files:[]}],
 sessions:[{workspace:'C:/fixture/current-worker',name:'Root agent',role:'root',
 model:'unknown',effort:'unknown',log_modified_utc:now,input_tokens:null,output_tokens:null}],tokens:null};
const context={console,Date,AbortSignal,location:{hostname:'127.0.0.1'},
 document:{querySelector:get,body:get('body'),hidden:false},
 setInterval(){},window:{},
 fetch:async(url,options)=>{
  requests.push({url,options});
  if(unavailable&&url==='/api/sprints/active')throw Error('offline');
  const data=url==='/api/launcher/options'?{profiles:[],lanes:[]}:
   url==='/api/progress'?(unavailable?{available:false,error:'Unavailable'}:progress):
   incomplete?{available:true,observation:'RECENT_ACTIVITY'}:activity;
  return {ok:true,json:async()=>data};
 }};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.match(get('#externalSprintContent').innerHTML,/current-worker/);
 assert.match(get('#externalSprintContent').innerHTML,/137/);
 assert.match(get('#externalSprintContent').innerHTML,/unavailable-worker/);
 assert.match(get('#externalSprintContent').innerHTML,/Unavailable|Unknown/);
 assert.doesNotMatch(get('#externalSprintContent').innerHTML,/0 input|0 output|0 uncommitted/);
 assert.match(get('#releaseContent').innerHTML,/183/);
 assert.match(get('#releaseContent').innerHTML,/C:\/fixture\/MJobs/);
 assert.match(get('#releaseRefreshTime').textContent,new RegExp(String(new Date().getFullYear())));
 assert.equal(requests.every(r=>r.options.cache==='no-store'),true);
 unavailable=true;
 await get('#missionRefresh').onclick();
 assert.match(get('#releaseRefreshTime').textContent,/Unavailable/i);
 assert.match(get('#externalSprintStatus').textContent,/unavailable/i);
 assert.doesNotMatch(get('#externalSprintContent').textContent,/137/);
 unavailable=false;incomplete=true;
 await get('#refreshBtn').handlers.click();
 assert.match(get('#externalSprintStatus').textContent,/unavailable/i);
 assert.doesNotMatch(get('#externalSprintContent').textContent,/0 recent|0 workspaces/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def test_current_workspaces_unknown_counts_and_failed_refresh():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the UI regression")
    script = Path(__file__).resolve().parents[2] / "tools/agent_dashboard/static/mission.js"
    result = subprocess.run([node, "-e", SCRIPT, str(script)], capture_output=True,
                            text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
