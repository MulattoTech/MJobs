'use strict';
// A bounded Enter-only completion of this caller's known Codex paste. Never
// resends text, chooses a menu item, handles an approval, or changes security.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),net=require('node:net'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');const exec=promisify(execFile);
const sleep=ms=>new Promise(ok=>setTimeout(ok,ms));
const clean=x=>String(x).replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'');
const norm=x=>x.replace(/\s+/g,'');
function composerMatches(surface,message){
 if(typeof message!=='string'||message.length<10||message.length>12000||/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(message))return false;
 const text=clean(surface),matches=[...text.matchAll(/(?:^|\n)\s*› /g)];if(!matches.length)return false;
 const start=matches.at(-1).index+matches.at(-1)[0].length;
 const body=text.slice(start).split(/\n\s*GPT-/)[0].trim();
 if(!body||body==='Ask Codex to do anything'||body==='1. Set up security')return false;
 const folded=[...body.matchAll(/\[Pasted\s+Content\s+(\d+)\s+chars\]/g)];
 if(!folded.length)return norm(body)===norm(message);
 if(folded.length>8||folded.some(m=>!Number.isSafeInteger(Number(m[1]))||Number(m[1])<1))return false;
 const parts=[];let end=0;for(const m of folded){parts.push({literal:body.slice(end,m.index),count:Number(m[1])});end=m.index+m[0].length;}parts.push({literal:body.slice(end),count:null});
 // Compare each visible segment to the known message in order. The UI can wrap
 // a paste marker, preserve a literal prefix/suffix, or create several folds.
 // Whitespace introduced only by rendering does not change the authored text.
 function matchesParts(expected){
  let visits=0;
  function walk(index,offset){
   if(++visits>256)return false;const part=parts[index];
   if(part.count===null)return norm(expected.slice(offset))===norm(part.literal);
   for(const char of norm(part.literal)){
    while(offset<expected.length&&/\s/.test(expected[offset]))offset++;
    if(expected[offset]!==char)return false;offset++;
   }
   const positions=[offset];while(offset<expected.length&&/\s/.test(expected[offset]))positions.push(++offset);
   return positions.some(pos=>pos+part.count<=expected.length&&walk(index+1,pos+part.count));
  }
  return walk(0,0);
 }
 return matchesParts(message)||matchesParts(message+'\n');
}
function frame(type,payload=Buffer.alloc(0)){const h=Buffer.alloc(5);h[0]=type;h.writeUInt32BE(payload.length,1);return Buffer.concat([h,payload]);}
function terminalTransport(port){
 return new Promise((resolve,reject)=>{
  const socket=net.createConnection({host:'127.0.0.1',port});let pending=null,buffer=Buffer.alloc(0),bytes=0,closed=false;
  const end=error=>{if(closed)return;closed=true;socket.destroy();if(pending){clearTimeout(pending.timer);pending.reject(error||Error('Terminal connection closed'));pending=null;}};
  const timer=setTimeout(()=>{end();reject(Error('Terminal connection timeout'));},2500);
  socket.on('error',e=>{clearTimeout(timer);end(e);reject(e);});
  socket.on('close',()=>end());
  socket.on('data',chunk=>{
   bytes+=chunk.length;if(bytes>24*1024*1024){end(Error('Terminal response exceeds limit'));return;}
   buffer=Buffer.concat([buffer,chunk]);
   while(buffer.length>=5){const size=buffer.readUInt32BE(1);if(size>20*1024*1024){end(Error('Terminal frame exceeds limit'));return;}if(buffer.length<size+5)return;const kind=buffer[0],payload=buffer.subarray(5,5+size);buffer=buffer.subarray(5+size);
    if(kind===10&&pending){if(size>524288){end(Error('Terminal surface exceeds limit'));return;}const item=pending;pending=null;clearTimeout(item.timer);item.resolve(payload.toString('utf8'));}
   }
  });
  socket.once('connect',()=>{clearTimeout(timer);resolve({capture:()=>new Promise((ok,no)=>{if(closed||pending){no(Error('Terminal read unavailable'));return;}pending={resolve:ok,reject:no,timer:setTimeout(()=>end(Error('Terminal surface timeout')),3500)};socket.write(frame(9,Buffer.from('{"lines":35}')));}),enter:()=>{if(closed)throw Error('Terminal unavailable');socket.write(frame(2,Buffer.from('\r')));},close:()=>end()});});
 });
}
function registryBinding(id,generation,home=os.homedir()){
 if(!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(id)||typeof generation!=='string'||!generation)throw Error('Terminal target identity invalid');
 const file=path.join(home,'.ao','windows-pty-hosts.json');if(fs.statSync(file).size>4*1024*1024)throw Error('Registry too large');const registry=JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''));
 const found=registry.filter(x=>x.sessionId===id);if(found.length!==1)throw Error('Terminal ownership ambiguous');const e=found[0];
 if(e.launchId!==generation||!Number.isInteger(e.ptyHostPid)||e.ptyHostPid<=0||!/^127\.0\.0\.1:\d{1,5}$/.test(e.pipePath))throw Error('Terminal ownership changed');const port=Number(e.pipePath.split(':')[1]);if(port<1||port>65535)throw Error('Terminal port invalid');if(!Number.isFinite(Date.parse(e.registeredAt||''))||Date.parse(e.registeredAt)>Date.now()+1000)throw Error('Terminal registration time invalid');return {id,generation,pid:e.ptyHostPid,port,registeredAt:e.registeredAt};
}
async function verifyOwner(binding,run=exec,home=os.homedir()){
 const native=`using System;using System.Runtime.InteropServices;public static class AOOwnerIdentityV1{[DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr CommandLineToArgvW(string value,out int count);[DllImport("kernel32.dll")]static extern IntPtr LocalFree(IntPtr value);public static string[] Parse(string value){int count;var memory=CommandLineToArgvW(value,out count);if(memory==IntPtr.Zero)throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());try{var result=new string[count];for(var i=0;i<count;i++)result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size));return result;}finally{LocalFree(memory);}}}`;
 const command=`$ErrorActionPreference='Stop';Add-Type -TypeDefinition '${native}';$p=Get-CimInstance Win32_Process -Filter 'ProcessId = ${binding.pid}';$c=@(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort ${binding.port} -State Listen);[ordered]@{image=$p.ExecutablePath;createdAt=([DateTimeOffset]$p.CreationDate).ToUniversalTime().ToString('o');owners=@($c.OwningProcess);argv=@([AOOwnerIdentityV1]::Parse($p.CommandLine))}|ConvertTo-Json -Compress`;
 const x=await run('C:\\Program Files\\PowerShell\\7\\pwsh.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',command],{windowsHide:true,shell:false,timeout:8000,maxBuffer:32000});const v=JSON.parse(x.stdout);
 const expected=path.join(home,'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe'),argv=Array.isArray(v.argv)?v.argv:[];
 let hostIndex=2;while(['--start=attach','--surface=lazy'].includes(argv[hostIndex]))hostIndex++;
 const values=flag=>argv.flatMap((value,index)=>value===flag&&index+1<argv.length?[argv[index+1]]:[]);
 if(v.owners?.length!==1||v.owners[0]!==binding.pid||String(v.image).toLowerCase()!==expected.toLowerCase()||!Number.isFinite(Date.parse(v.createdAt||''))||Math.abs(Date.parse(v.createdAt)-Date.parse(binding.registeredAt))>15000||argv[1]!=='pty-host'||argv[hostIndex]!==binding.id||values('--session').length!==1||values('--session')[0]!==binding.id||values('--launch').length!==1||values('--launch')[0]!==binding.generation)throw Error('Terminal listener ownership not verified');
}
const isIdle=s=>s&&!s.isTerminated&&s.mode==='tui'&&s.harness==='codex'&&['idle','waiting_input'].includes(s.activity?.state);
async function completeKnownPaste({root,id,generation,message,sentAt,getSession,guard,transport=terminalTransport,bindingReader=registryBinding,ownerCheck=verifyOwner,wait=sleep,receipt=null}){
 if(!Number.isFinite(sentAt)||sentAt>Date.now()+1000||typeof guard!=='function'||typeof getSession!=='function')throw Error('Invalid delivery verification');
 let channel;
 const confirmed=async()=>{if(typeof receipt!=='function')return null;const proof=await receipt();return proof?.verified===true&&proof.sessionId===id&&proof.messageHash===crypto.createHash('sha256').update(message).digest('hex')?proof:null;};
 try{
  await wait(1100);await guard();let s=await getSession();if(s?.isTerminated||s?.terminalGeneration!==generation)throw Error('Native generation changed after send');
  if(typeof receipt==='function'){for(let i=0;i<8;i++){const proof=await confirmed();if(proof)return {state:'native-observed',extraEnter:false,receipt:proof};await wait(350);await guard();s=await getSession();if(s?.isTerminated||s?.terminalGeneration!==generation)throw Error('Native identity changed while reading receipt');}}
  if(s.activity?.state==='active'||Date.parse(s.activity?.lastActivityAt||'')>sentAt+100)return {state:'decision-or-unavailable',extraEnter:false};
  if(!isIdle(s))return {state:'decision-or-unavailable',extraEnter:false};
  const binding=bindingReader(id,generation);await ownerCheck(binding);channel=await transport(binding.port);
  const before=await channel.capture();if(!composerMatches(before,message))return {state:'no-matching-pending-composer',extraEnter:false};
  await wait(200);const second=await channel.capture();if(clean(before)!==clean(second)||!composerMatches(second,message))throw Error('Composer changed during verification');
  await guard();s=await getSession();if(!isIdle(s)||s.terminalGeneration!==generation||Date.parse(s.activity?.lastActivityAt||'')>sentAt+100)throw Error('Native activity changed before Enter');
  if(JSON.stringify(bindingReader(id,generation))!==JSON.stringify(binding))throw Error('Terminal registry changed before Enter');
  const directory=path.join(root,'diagnostics','terminal-deliveries');fs.mkdirSync(directory,{recursive:true});
  const key=crypto.createHash('sha256').update(JSON.stringify([id,generation,sentAt,message])).digest('hex'),file=path.join(directory,key+'.json');
  const record={at:new Date().toISOString(),id,generation,sentAt,messageSha256:crypto.createHash('sha256').update(message).digest('hex'),action:'one-enter-only',state:'reserved'};
  try{fs.writeFileSync(file,JSON.stringify(record),{flag:'wx'});}catch(e){if(e.code==='EEXIST')return {state:'already-attempted',extraEnter:false};throw e;}
  await guard();channel.enter();record.state='sent-awaiting-native';fs.writeFileSync(file,JSON.stringify(record));
  for(let i=0;i<16;i++){await wait(250);const current=await getSession();if(current?.isTerminated||current?.terminalGeneration!==generation)throw Error('Native generation changed after Enter');const proof=await confirmed();if(proof){record.state='native-observed';record.observedAt=new Date().toISOString();record.receipt=proof;fs.writeFileSync(file,JSON.stringify(record));return {state:'native-observed',extraEnter:true,receipt:proof};}}
  return {state:'enter-outcome-pending',extraEnter:true};
 }finally{channel?.close();}
}
module.exports={composerMatches,completeKnownPaste,registryBinding,terminalTransport,verifyOwner};
