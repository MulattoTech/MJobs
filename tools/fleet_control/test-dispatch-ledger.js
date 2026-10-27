'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {DispatchLedger}=require('./dispatch-ledger');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-ledger-test-'));
try{
 let now=Date.now();const x=new DispatchLedger({root,now:()=>now});const a=x.reserve('worker-1','first');
 assert.throws(()=>x.reserve('worker-1','different instruction'),/unverified/);
 x.finish('worker-1',a.id,'uncertain');
 assert.throws(()=>new DispatchLedger({root,now:()=>now}).reserve('worker-1','retry'),/unverified/);
 assert.ok(x.reserve('worker-2','parallel worker').id);
 now+=120001;assert.throws(()=>x.reserve('worker-1','next'),/unverified/,'Elapsed time is not receipt evidence');
 now+=2*86400000;x.save();const recovered=new DispatchLedger({root,now:()=>now});assert(recovered.entries['worker-1']);assert.throws(()=>recovered.reserve('worker-1','after restart'),/unverified/);
 recovered.finish('worker-1',a.id,'submitted');assert.ok(recovered.reserve('worker-1','next distinct instruction').id);
 console.log('PASS: uncertain reservations survive expiry/restart; only confirmed delivery releases the hold; other workers remain independent.');
}finally{fs.rmSync(root,{recursive:true,force:true});}
