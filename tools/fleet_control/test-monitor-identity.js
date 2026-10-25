'use strict';
const assert=require('node:assert/strict'),path=require('node:path'),{execFileSync}=require('node:child_process');
const {assertReadOnlyRPC}=require('./quota-guard');
for(const method of ['initialize','initialized','account/rateLimits/read'])assert.equal(assertReadOnlyRPC({method}),true);
for(const method of ['turn/start','thread/start','turn/steer','model/list','account/login/start',null,''])assert.throws(()=>assertReadOnlyRPC({method}),/non-metering/);
assert.throws(()=>assertReadOnlyRPC(null),/non-metering/);
const output=execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',['-NoProfile','-NonInteractive','-File',path.join(__dirname,'test-monitor-identity.ps1')],{windowsHide:true,timeout:30000,encoding:'utf8'});
process.stdout.write(output);console.log('PASS outbound quota transport rejects inference and arbitrary RPC');
