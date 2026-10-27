'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {composerMatches,completeKnownPaste}=require('./terminal-delivery');
const message='USER-AUTHORIZED PRODUCTIVE MAINTENANCE: Read exact source services/example.py and produce a cited private draft. Do not change code or security.';
function surface(body){return 'Historical completed work\n  Set up security for Daybreak mode\n  You can keep using Codex while you finish setup.\n  Press a number to choose · esc to dismiss · type to continue\n› '+body+'\n  GPT-5.6-Sol medium · workspace';}
function fixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'terminal-delivery-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const sentAt=Date.now()-10000;let enters=0,closed=0,guards=0,reads=0,ready=false,text=surface(message);let session={id:'worker-1',mode:'tui',harness:'codex',isTerminated:false,terminalGeneration:'generation-1',activity:{state:'idle',lastActivityAt:new Date(sentAt-10000).toISOString()}};
 const options={root,id:'worker-1',generation:'generation-1',message,sentAt,getSession:async()=>session,guard:()=>{guards++;},bindingReader:()=>({id:'worker-1',generation:'generation-1',pid:123,port:456}),ownerCheck:async()=>{},wait:async()=>{},transport:async()=>({capture:async()=>{reads++;return text;},enter:()=>{enters++;if(ready)session={...session,activity:{state:'active',lastActivityAt:new Date().toISOString()}};},close:()=>{closed++;}})};
 return {options,setText:x=>{text=x;},setSession:x=>{session={...session,...x};},confirm:()=>{ready=true;},counts:()=>({enters,closed,guards,reads})};}
test('normal and split/full collapsed known composers match; notice menus and unrelated drafts do not',()=>{
 assert(composerMatches(surface(message),message));assert(composerMatches(surface('[Pasted Content 60 chars]'+message.slice(60)),message));assert(composerMatches(surface('[Pasted Content '+message.length+' chars]'),message));assert(composerMatches(surface('[Pasted Content '+(message.length+1)+' chars]'),message));
 for(const body of ['Ask Codex to do anything','1. Set up security','Approve execution?','unrelated user draft','[Pasted Content 10 chars]wrong','[Pasted Content 99999 chars]'])assert.equal(composerMatches(surface(body),message),false,body);
 assert.equal(composerMatches(surface(message),'bad\x1bmessage'),false);
});
test('already active/recorded native turn never receives a new Enter',async t=>{const f=fixture(t);f.setSession({activity:{state:'active',lastActivityAt:new Date().toISOString()}});const r=await completeKnownPaste(f.options);assert.equal(r.extraEnter,false);assert.equal(f.counts().enters,0);assert.equal(f.counts().reads,0);});
test('verified pending paste receives one Enter and is observed natively; text is never resent',async t=>{const f=fixture(t);f.confirm();const r=await completeKnownPaste(f.options);assert.deepEqual(r,{state:'native-observed',extraEnter:true});assert.equal(f.counts().enters,1);assert.equal(f.counts().reads,2);assert.equal(f.counts().closed,1);const files=fs.readdirSync(path.join(f.options.root,'diagnostics','terminal-deliveries'));assert.equal(files.length,1);const record=JSON.parse(fs.readFileSync(path.join(f.options.root,'diagnostics','terminal-deliveries',files[0])));assert.equal(record.state,'native-observed');assert.equal(JSON.stringify(record).includes(message),false);});
test('unknown result is durably fenced and never auto-entered twice',async t=>{const f=fixture(t);const first=await completeKnownPaste(f.options);assert.equal(first.state,'enter-outcome-pending');const second=await completeKnownPaste(f.options);assert.equal(second.state,'already-attempted');assert.equal(f.counts().enters,1);});
test('blocked decision, changed generation, foreign listener, changed composer and stop guard prevent input',async t=>{
 for(const kind of ['decision','generation','owner','composer','guard','registry'])await t.test(kind,async t=>{const f=fixture(t);f.confirm();let n=0;
  if(kind==='decision')f.setSession({activity:{state:'blocked'}});
  if(kind==='generation')f.setSession({terminalGeneration:'other'});
  if(kind==='owner')f.options.ownerCheck=async()=>{throw Error('Foreign listener');};
  if(kind==='composer')f.options.transport=async()=>({capture:async()=>++n===1?surface(message):surface('user modified draft'),enter:()=>{throw Error('must not enter');},close(){}});
  if(kind==='guard')f.options.guard=()=>{if(++n===3)throw Error('Emergency stop');};
  if(kind==='registry')f.options.bindingReader=()=>({id:'worker-1',generation:'generation-1',pid:++n===1?123:124,port:456});
  try{await completeKnownPaste(f.options);}catch(e){assert(e.message);}
  assert.equal(f.counts().enters,0,kind);
 });
});
test('a different or empty composer is left unchanged without a submission fence',async t=>{const f=fixture(t);f.setText(surface('Ask Codex to do anything'));const r=await completeKnownPaste(f.options);assert.equal(r.state,'no-matching-pending-composer');assert.equal(f.counts().enters,0);assert.equal(fs.existsSync(path.join(f.options.root,'diagnostics')),false);});

test('literal prefixes and wrapped/multiple paste markers retain the same known task order',()=>{
 const prefix=message.slice(0,60),rest=message.length-60;
 assert(composerMatches(surface(prefix+'[Pasted\n  Content '+rest+' chars]'),message));
 assert(composerMatches(surface(message.slice(0,20)+'[Pasted Content 10 chars]'+message.slice(30,60)+'[Pasted Content '+rest+' chars]'),message));
 assert.equal(composerMatches(surface('wrong prefix[Pasted Content '+rest+' chars]'),message),false);
 assert.equal(composerMatches(surface(prefix+'[Pasted Content '+(rest+5)+' chars]'),message),false);
 assert.equal(composerMatches(surface(prefix+'[Pasted Content '+rest+' chars]unrelated suffix'),message),false);
});

test('terminal registry binds one generation and a local listener, never an arbitrary endpoint',t=>{
 const {registryBinding}=require('./terminal-delivery');const home=fs.mkdtempSync(path.join(os.tmpdir(),'terminal-registry-'));t.after(()=>fs.rmSync(home,{recursive:true,force:true}));fs.mkdirSync(path.join(home,'.ao'));const file=path.join(home,'.ao/windows-pty-hosts.json');const entry={sessionId:'worker-1',launchId:'generation-1',ptyHostPid:123,pipePath:'127.0.0.1:4567',registeredAt:new Date(Date.now()-60000).toISOString()};
 const write=rows=>fs.writeFileSync(file,JSON.stringify(rows));write([entry]);assert.equal(registryBinding('worker-1','generation-1',home).port,4567);
 for(const change of [{launchId:'other'},{pipePath:'public.example:4567'},{pipePath:'127.0.0.1:0'},{ptyHostPid:0},{registeredAt:'invalid'},{registeredAt:'2099-01-01T00:00:00Z'}]){write([{...entry,...change}]);assert.throws(()=>registryBinding('worker-1','generation-1',home));}
 write([entry,entry]);assert.throws(()=>registryBinding('worker-1','generation-1',home),/ambiguous/);
});
