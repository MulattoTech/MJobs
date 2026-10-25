'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {normalizeAgyStatusline,saveAgyObservation,readGeminiUsage}=require('./gemini-usage');
const now=Date.parse('2026-10-24T23:00:00Z'),root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-quota-fixture-')),cachePath=path.join(root,'quota.json');
const payload={product:'antigravity',email:'NEVER_SAVE',transcript_path:'NEVER_SAVE',conversation_id:'NEVER_SAVE',quota:{'gemini-weekly':{remaining_fraction:.9378},'gemini-five-hour':{remaining_fraction:0},disabled:{disabled:true,remaining_fraction:1},bad:{remaining_fraction:'0.9'},amount:{remaining_amount:17},excess:{remaining_fraction:1.2,remaining_amount:-1}}};
try{
 const s=normalizeAgyStatusline(payload,now);assert.equal(s.windows[0].remainingPercent,93.78);assert.equal(s.windows[1].remainingPercent,0);assert.equal(s.windows[2].remainingPercent,null);assert.equal(s.windows[3].remainingPercent,null);assert.equal(s.windows[4].remainingAmount,17);assert.equal(s.windows[4].remainingPercent,null);assert.equal(s.windows[5].remainingPercent,null);
 assert(!JSON.stringify(s).includes('NEVER_SAVE'));assert.throws(()=>normalizeAgyStatusline({product:'other',quota:{}}));assert.throws(()=>normalizeAgyStatusline({product:'antigravity',quota:[]}));
 let r=readGeminiUsage({agyAvailable:true},{cachePath,now});assert.equal(r.providers[0].remainingPercent,null);assert.equal(r.providers[1].status,'not_detected');
 saveAgyObservation(payload,{cachePath,now});r=readGeminiUsage({agyAvailable:true},{cachePath,now});assert.equal(r.providers[0].remainingPercent,0);assert.equal(r.providers[0].sampledAt,null);assert.equal(r.providers[0].automaticStopSupported,false);
 r=readGeminiUsage({agyAvailable:true},{cachePath,now:now+91000});assert.equal(r.providers[0].status,'stale');assert.equal(r.providers[0].remainingPercent,null);
 r=readGeminiUsage({agyAvailable:true},{cachePath,now:now-10000});assert.equal(r.providers[0].remainingPercent,null);
 saveAgyObservation(payload,{cachePath,now:now+1000});assert.equal(JSON.parse(fs.readFileSync(cachePath)).observedAt,new Date(now).toISOString());
 const changed=structuredClone(payload);changed.quota['gemini-weekly'].remaining_fraction=.81;saveAgyObservation(changed,{cachePath,now:now+1500});assert.equal(JSON.parse(fs.readFileSync(cachePath)).windows[0].remainingPercent,81);
 assert(!fs.readFileSync(cachePath,'utf8').includes('NEVER_SAVE'));
 saveAgyObservation({product:'antigravity',quota:{requests:{remaining_amount:17}}},{cachePath,now:now+2000});r=readGeminiUsage({agyAvailable:true},{cachePath,now:now+2000});assert.equal(r.providers[0].status,'observed');assert.equal(r.providers[0].remainingPercent,null);
 fs.writeFileSync(cachePath,'not JSON');assert.equal(readGeminiUsage({agyAvailable:true},{cachePath,now}).providers[0].remainingPercent,null);
 console.log('PASS native quota zero/unknown distinction, stale/future rejection, amounts, sanitization and changed-sample persistence');
}finally{fs.rmSync(root,{recursive:true,force:true});}
