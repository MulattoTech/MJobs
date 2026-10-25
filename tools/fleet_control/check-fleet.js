'use strict';
// Deliberately excludes browser tests that might click live action buttons.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{execFile}=require('node:child_process'),{promisify}=require('node:util');
const exec=promisify(execFile);
const testFiles=['test-dispatch-safety.js','test-dispatch-ledger.js','test-control-concurrency.js','test-stale-dispatch.js','test-quota.js','test-quota-freshness.js','test-recovery-7pct.js','test-turbo.js','test-all-hands.js','test-sustain.js','test-active-turn-observation.js','test-find-work.js','test-master.js','test-restore.js','test-restore-replacements.js','test-paused-manifest.js','test-stale-watchdog.js','test-agent-plans.js','test-agent-plan-apply.js','test-provider-catalog.js','test-preflight.js','test-monitor-identity.js','test-gemini-usage.js','test-dashboard-refresh.js'];
function sourceFingerprint(root){
 const files=[...fs.readdirSync(root).filter(f=>/\.(js|ps1)$/.test(f)),...fs.readdirSync(path.join(root,'public')).filter(f=>/\.(js|css|html)$/.test(f)).map(f=>'public/'+f)].sort();
 const hash=crypto.createHash('sha256');for(const file of files){hash.update(file+'\0');hash.update(fs.readFileSync(path.join(root,file)));}return hash.digest('hex');
}
async function run(root=__dirname){
 const rows=[],before=sourceFingerprint(root);
 for(const file of testFiles){
  if(!fs.existsSync(path.join(root,file))){rows.push({file,status:'NOT_RUN',reason:'File unavailable'});continue;}
  const start=Date.now();try{
   const x=await exec(process.execPath,[path.join(root,file)],{cwd:root,windowsHide:true,timeout:45000,maxBuffer:300000});
   rows.push({file,status:'PASS',ms:Date.now()-start,output:(x.stdout||'').trim().slice(-1200)});
  }catch(e){rows.push({file,status:'FAIL',ms:Date.now()-start,output:String(e.stderr||e.message).slice(-2000)});}
 }
 const after=sourceFingerprint(root);
 const result={at:new Date().toISOString(),sourceFingerprint:after,sourceStable:before===after,passed:rows.filter(x=>x.status==='PASS').length,failed:rows.filter(x=>x.status==='FAIL').length,notRun:rows.filter(x=>x.status==='NOT_RUN').length,tests:rows};
 result.ok=result.failed===0&&result.notRun===0&&result.sourceStable;
 const directory=path.join(root,'diagnostics');fs.mkdirSync(directory,{recursive:true});
 const output=path.join(directory,'preflight-results.json');fs.writeFileSync(output+'.tmp',JSON.stringify(result,null,2));fs.renameSync(output+'.tmp',output);
 for(const row of rows)console.log(row.status+' '+row.file);
 console.log(JSON.stringify({ok:result.ok,passed:result.passed,failed:result.failed,notRun:result.notRun,sourceStable:result.sourceStable}));
 if(!result.ok)process.exitCode=1;return result;
}
if(require.main===module)run().catch(e=>{console.error(e.message);process.exitCode=1});
module.exports={sourceFingerprint,run};
