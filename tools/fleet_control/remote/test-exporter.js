'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { SOURCES, projectSnapshot, collectSnapshot, writeSnapshot } = require('./export-status');
const { freshness, displayModel } = require('./public/app');

const NOW = Date.parse('2026-10-26T01:00:00Z');
const ISO = new Date(NOW).toISOString();
function fixture() {
  const data = {
    fleetHealth: {ok:true,dispatchBlockedReason:null},
    fleetState: {timestamp:ISO,summary:{total:2,working:1,idle:1},sessions:[
      {id:'mjobs-1',name:'Developer',model:'gpt-6.1',harness:'codex',role:'Implementation',busy:true,isIdle:false,activity:'working',controllerState:'busy',controllerVerified:true,openPRs:1,prompt:'SECRET_PROMPT',path:'C:\\private',token:'SECRET_TOKEN'},
      {id:'mjobs-2',name:'Reviewer',model:'gpt-6',harness:'codex',role:'Review',busy:false,isIdle:true,activity:'idle',controllerState:'ready',controllerVerified:true},
    ]},
    fleetMaster: {timestamp:ISO,dispatch:{sustainedEnabled:true,turboEnabled:false},emergency:{latched:false},restore:{phase:'completed',active:false},startup:{phase:'completed',active:false,reconciliationRequired:false,ownerId:'PRIVATE_OWNER',error:null},currentReadiness:{at:ISO,total:2,verified:2,ready:true,runtimesReady:true,dispatchPaused:false,healthyIds:['SECRET_NATIVE_ID']}},
    quota: {fresh:true,monitorOnline:true,sampledAt:ISO,selectedRemainingPercent:88,guardAcknowledged:true,config:{autoKillArmed:true,thresholdPercent:7},windows:[{label:'5 hours',remainingPercent:88,resetsAt:'2026-10-26T05:00:00Z'}],token:'SECRET_QUOTA'},
    missionHealth: {service:'mjobs-mission-control',status:'ready',root:'C:\\private'},
    missionState: {time:ISO,lanes:{codex:{config:{model:'gpt-6.1',reasoning_effort:'high',prompt_override:'SECRET_OVERRIDE'},runtime:{status:'running',pid:999},activity:{state:'working',process_tree:[{command:'SECRET_COMMAND'}]},log_tail:'SECRET_LOG',command:'SECRET_COMMAND'}}},
  };
  return Object.fromEntries(Object.entries(data).map(([name,value])=>[name,{available:true,checkedAt:ISO,data:value}]));
}

test('only operational allowlisted fields leave the local control planes',()=>{
  const snapshot=projectSnapshot(fixture(),NOW),serialized=JSON.stringify(snapshot);
  for(const forbidden of ['SECRET_','PRIVATE_OWNER','C:\\private','process_tree','log_tail','prompt_override','"token"','"pid"'])assert.equal(serialized.includes(forbidden),false,forbidden);
  assert.equal(snapshot.readOnly,true);assert.equal(snapshot.dispatch.state,'enabled');
  assert.equal(snapshot.fleet.summary.working,1);assert.equal(snapshot.quota.remainingPercent,88);
  assert.equal(snapshot.missionControl.lanes[0].model,'gpt-6.1, gpt-6');
  assert.equal(snapshot.missionControl.lanes[0].agents,2);
  assert.equal(snapshot.missionControl.lanes[0].status,'WORKING');
  assert.equal(displayModel(snapshot,NOW).verified,'2 / 2');
});

test('quota armed with a full allowance is distinct from working or dispatch enabled',()=>{
  const data=fixture();data.quota.data.selectedRemainingPercent=100;data.fleetMaster.data.dispatch.sustainedEnabled=false;
  data.fleetState.data.summary.working=0;for(const session of data.fleetState.data.sessions){session.busy=false;session.activity='idle';}
  const snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.quota.armed,true);assert.equal(snapshot.quota.remainingPercent,100);
  assert.equal(snapshot.dispatch.state,'paused');assert.equal(snapshot.fleet.summary.working,0);
});

test('enabled scheduler GitHub holds are shown truthfully without exporting raw errors',()=>{
  const data=fixture();
  data.fleetHealth.data.dispatchBlockedReason='GitHub snapshot unavailable or stale; no automatic assignments';
  let snapshot=projectSnapshot(data,NOW);
  assert.equal(snapshot.dispatch.state,'blocked');
  assert.equal(snapshot.dispatch.sustainedEnabled,true,'configured enabled remains distinct from able to dispatch');
  assert.equal(snapshot.dispatch.reason,'GitHub repository inventory is unavailable or stale');
  data.fleetHealth.data.dispatchBlockedReason='GitHub snapshot unavailable or stale; no automatic assignments: private token C:\\secret';
  snapshot=projectSnapshot(data,NOW);
  assert.equal(snapshot.dispatch.reason,'Local control plane reports a dispatch hold');
  assert.equal(JSON.stringify(snapshot).includes('private token'),false);
  assert.equal(JSON.stringify(snapshot).includes('C:\\secret'),false);
  data.fleetHealth.data.dispatchBlockedReason=null;
  assert.equal(projectSnapshot(data,NOW).dispatch.state,'enabled','a recovered planner removes the display hold');
});

test('failed sources never reuse previous healthy metrics or lanes',()=>{
  const data=fixture();for(const name of Object.keys(data))data[name]={available:false,error:'UNAVAILABLE',data:data[name].data};
  const snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.dispatch.state,'unknown');
  assert.equal(snapshot.fleet.runtimeAvailable,false);assert.equal(snapshot.fleet.summary.working,null);
  assert.equal(snapshot.quota.fresh,false);assert.equal(snapshot.quota.remainingPercent,null);assert.equal(snapshot.quota.armed,null);
  assert.deepEqual(snapshot.fleet.sessions,[]);assert.deepEqual(snapshot.missionControl.lanes,[]);
});

test('preserved offline rosters cannot appear as a running fleet',()=>{
  const data=fixture();data.fleetState.data.offline=true;
  const snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.fleet.runtimeAvailable,false);
  assert.equal(snapshot.fleet.preservedSnapshot,true);assert.equal(snapshot.fleet.summary.working,0);
  assert.equal(snapshot.dispatch.state,'unknown');assert.equal(snapshot.fleet.sessions.some(session=>session.busy),false);
  assert.equal(snapshot.fleet.sessions.some(session=>session.controllerVerified===true),false);
});

test('stale quota does not publish numeric allowance or historical quota windows',()=>{
  const data=fixture();data.quota.data.fresh=false;
  const snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.quota.remainingPercent,null);assert.deepEqual(snapshot.quota.windows,[]);
});

test('an old provider sample cannot gain freshness from a new exporter timestamp',()=>{
  const data=fixture();data.quota.data.sampledAt=new Date(NOW-46000).toISOString();
  const snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.quota.fresh,false);assert.equal(snapshot.quota.remainingPercent,null);
});

test('pending or stale runtime inspection never claims verified healthy controllers',()=>{
  const data=fixture();data.fleetMaster.data.currentReadiness.inspectionPending=true;
  let snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.readiness.available,false);assert.equal(snapshot.readiness.verified,null);
  assert.equal(displayModel(snapshot,NOW).verified,'—');
  data.fleetMaster.data.currentReadiness.inspectionPending=false;data.fleetMaster.data.currentReadiness.at=new Date(NOW-46000).toISOString();
  snapshot=projectSnapshot(data,NOW);assert.equal(snapshot.readiness.available,false);assert.equal(snapshot.readiness.runtimesReady,null);
});

test('UI clears live claims after the exporter stops or the clock is invalid',()=>{
  const snapshot=projectSnapshot(fixture(),NOW);
  assert.equal(freshness(snapshot,NOW+44000).stale,false);
  const display=displayModel(snapshot,NOW+46000);assert.equal(display.stale,true);
  assert.equal(display.dispatch,'Unknown');assert.equal(display.working,'—');assert.equal(display.quota,'—');
  assert.deepEqual(display.sessions,[]);assert.deepEqual(display.lanes,[]);
  assert.equal(freshness({...snapshot,generatedAt:'invalid'},NOW).stale,true);
  assert.equal(freshness({...snapshot,generatedAt:new Date(NOW+20000).toISOString()},NOW).stale,true);
});

test('UI expires provider and inspector observations before a newer publication expires',()=>{
  const data=fixture();
  data.quota.data.sampledAt=new Date(NOW-40000).toISOString();
  data.fleetMaster.data.currentReadiness.at=new Date(NOW-40000).toISOString();
  const snapshot=projectSnapshot(data,NOW);
  assert.equal(displayModel(snapshot,NOW+4000).quota,'88%');
  assert.equal(displayModel(snapshot,NOW+4000).verified,'2 / 2');
  const later=displayModel(snapshot,NOW+6000);
  assert.equal(later.stale,false,'publication remains fresh');
  assert.equal(later.quota,'—','provider observation expired independently');
  assert.equal(later.verified,'—','runtime inspection expired independently');
});

test('exporter uses only fixed loopback GET routes and rejects redirected/error/stale responses',async()=>{
  const data=fixture(),calls=[];
  const fakeFetch=async(url,options)=>{
    calls.push({url,options});const name=Object.keys(SOURCES).find(key=>SOURCES[key]===url);
    if(name==='quota')return new Response('login html',{status:302});
    const item=structuredClone(data[name].data);if(name==='fleetState')item.timestamp='2026-10-25T01:00:00Z';
    return new Response(JSON.stringify(item),{headers:{'content-type':'application/json'}});
  };
  const snapshot=await collectSnapshot(fakeFetch,()=>NOW);
  assert.equal(calls.length,6);for(const call of calls){assert.equal(new URL(call.url).hostname,'127.0.0.1');assert.equal(call.options.method,'GET');assert.equal(call.options.redirect,'error');}
  assert.equal(snapshot.sources.fleetState.error,'STALE_SOURCE');assert.equal(snapshot.fleet.runtimeAvailable,false);
  assert.equal(snapshot.sources.quota.available,false);assert.equal(snapshot.quota.remainingPercent,null);
});

test('bounded master retries observe a settling runtime inspection using one shared deadline',async()=>{
  const data=fixture(),masterSignals=[];let masterCalls=0;
  const snapshot=await collectSnapshot(async(url,options)=>{
    const name=Object.keys(SOURCES).find(key=>SOURCES[key]===url),item=structuredClone(data[name].data);
    if(name==='fleetMaster'){
      masterCalls++;masterSignals.push(options.signal);
      item.currentReadiness.inspectionPending=masterCalls<3;
      if(masterCalls<3){item.currentReadiness.stale=true;item.currentReadiness.verified=0;}
    }
    return new Response(JSON.stringify(item));
  },()=>NOW);
  assert.equal(masterCalls,3);assert.equal(masterSignals.every(signal=>signal===masterSignals[0]),true);
  assert.equal(snapshot.readiness.available,true);assert.equal(snapshot.readiness.verified,2);
});

test('persistent pending inspection stops after three GETs and remains unverified',async()=>{
  const data=fixture();let masterCalls=0;
  const snapshot=await collectSnapshot(async url=>{
    const name=Object.keys(SOURCES).find(key=>SOURCES[key]===url),item=structuredClone(data[name].data);
    if(name==='fleetMaster'){masterCalls++;item.currentReadiness.inspectionPending=true;}
    return new Response(JSON.stringify(item));
  },()=>NOW);
  assert.equal(masterCalls,3);assert.equal(snapshot.readiness.available,false);
  assert.equal(snapshot.readiness.verified,null);assert.equal(snapshot.readiness.runtimesReady,null);
  assert.equal(displayModel(snapshot,NOW).verified,'—');
});

test('response size and malformed payloads fail closed without exporting response text',async()=>{
  const snapshot=await collectSnapshot(async()=>new Response('SECRET_BODY',{headers:{'content-length':String(3*1024*1024)}}),()=>NOW);
  assert.equal(Object.values(snapshot.sources).every(source=>source.error==='RESPONSE_TOO_LARGE'),true);
  assert.equal(JSON.stringify(snapshot).includes('SECRET_BODY'),false);
  const invalid=await collectSnapshot(async()=>new Response('{broken SECRET_BODY'),()=>NOW);
  assert.equal(Object.values(invalid.sources).every(source=>!source.available),true);
});

test('snapshot replacement is atomic and leaves no temporary files',()=>{
  const output=fs.mkdtempSync(path.join(os.tmpdir(),'mjobs-remote-'));
  try{writeSnapshot(output,projectSnapshot(fixture(),NOW));writeSnapshot(output,projectSnapshot({},NOW+10000));
    const value=JSON.parse(fs.readFileSync(path.join(output,'status.json'),'utf8'));
    assert.equal(value.generatedAt,new Date(NOW+10000).toISOString());assert.equal(value.fleet.runtimeAvailable,false);
    assert.deepEqual(fs.readdirSync(output),['status.json']);
  }finally{fs.rmSync(output,{recursive:true,force:true});}
});
