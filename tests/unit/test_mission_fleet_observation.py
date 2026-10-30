"""Inert browser-source tests for truthful Mission Control observations."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const scenario=process.argv[2];
let clock=100000;
class FixedDate extends Date { static now(){return clock;} }
class Element {
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.textContent='';this.children=[];this.disabled=false;}
 append(...items){this.children.push(...items);}
 prepend(item){this.children.unshift(item);}
 replaceChildren(...items){this.children=[...items];}
 insertAdjacentElement(_where,item){this.children.push(item);}
 addEventListener(){}
}
const elements=new Map(),requests=[],timers=[];
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const health={ok:true,turboEnabled:scenario==='turbo'||scenario==='conflict',sustainedEnabled:scenario==='steady'||scenario==='conflict'};
const snapshot={offline:scenario==='offline',sessions:[
 {id:'active',harness:'codex',activity:'active',busy:true},
 {id:'idle',harness:'codex',activity:'idle',busy:false},
 {id:'old',harness:'codex',activity:'exited',busy:false},
 {id:'terminated',harness:'codex',activity:'active',isTerminated:true,busy:true},
 {id:'gemini',harness:'gemini',activity:'active',busy:true}
],summary:{total:99,working:99}};
const context={Date:FixedDate,console,AbortSignal,document:{
 hidden:false,activeElement:null,body:new Element('body'),
 createElement:tag=>new Element(tag),getElementById:get,
 querySelector:()=>new Element('main'),addEventListener(){}
},setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;},
fetch:async(url,options={})=>{
 requests.push({url,method:options.method||'GET'});
 if(options.method==='POST'){
   assert.equal(url,'/fleet/api/master/pause');
   health.turboEnabled=false;health.sustainedEnabled=false;
   return {ok:true,json:async()=>({ok:true})};
 }
 const value=url==='/fleet/api/state'?snapshot:
 url==='/fleet/api/startup'?{readiness:{at:new Date(clock).toISOString(),total:3,verified:3,ready:true}}:
 url==='/fleet/api/health'?health:null;
 assert.notEqual(value,null,'unexpected route '+url);
 return {ok:true,json:async()=>value};
}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(requests.filter(x=>x.method==='POST').length,0,'page load must not dispatch');
 const expected={turbo:'Mode: Turbo',steady:'Mode: Steady',paused:'Mode: Paused',conflict:'Mode unavailable',offline:'Mode unavailable'}[scenario];
 assert.equal(get('fleet-work-mode').textContent,expected);
 const provider=context.MJobsFleetObservation.provider('codex');
 if(scenario==='offline'){
   assert.equal(provider.available,false);assert.equal(provider.total,0);
 }else{
   assert.equal(provider.total,2);assert.equal(provider.working,1);
   assert.match(get('live-fleet-summary').textContent,/3 managed sessions; 2 working/);
   assert.doesNotMatch(get('live-fleet-summary').textContent,/99/);
 }
 if(scenario==='turbo'){
   await get('fleet-pause').onclick();
   assert.equal(requests.filter(x=>x.method==='POST').length,1);
   assert.equal(get('fleet-work-mode').textContent,'Mode: Paused');
   clock+=26000;
   timers.find(x=>x.ms===5000).fn();
   assert.equal(get('fleet-work-mode').textContent,'Mode unavailable');
   assert.equal(context.MJobsFleetObservation.provider('codex').available,false);
 }
 assert.equal(requests.some(x=>x.url.includes('/sustain/')),false);
 console.log(JSON.stringify({scenario,mode:get('fleet-work-mode').textContent,posts:requests.filter(x=>x.method==='POST').length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize("scenario", ["turbo", "steady", "paused", "conflict", "offline"])
def test_mission_observations_and_single_mode_owner(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the browser-source regression")
    source = ROOT / "tools/agent_dashboard/static/fleet-runtime.js"
    result = subprocess.run(
        [node, "-e", SCRIPT, str(source), scenario],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["scenario"] == scenario
