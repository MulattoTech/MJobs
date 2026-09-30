import React, { useEffect, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { api, eligible, terminal } from './api';
import './style.css';

type Fact = { id: string; fact_id?: string; kind: string; text: string; source: string; confidence: number; verified: boolean; user_approved: boolean };
type Answer = Omit<Fact, 'kind' | 'fact_id'>;
type Profile = {id:string; name:string; email:string; version?:string; facts:Fact[]; answers:Answer[]};
type Match = {score:number; missing:string[]; components:{method?:string; matched?:string[]; detected?:string[]; limitation?:string}};
type Job = {id:string; title:string; company:string; provider:string; location:string; remote_type:string; description:string; skills:string[]; application_url:string; match_score:number|null; posting_count:number; attributes:{fictional?:boolean; salary?:{min:number;max:number;currency:string;period:string}[]; last_collected_at?:string}; match?:Match|null; postings?:{source:string;source_job_id:string;fetched_at:string;snapshots:{sha256:string;retrieved_at:string}[]}[]};
type Application = {id:string; title:string; company:string; state:string; target:string; job_id:string; profile_id:string; document_version:number; profile_version:string; fact_ids:string[]; trace:string[]; confirmation:Record<string,unknown>|null; attempts?:{state:string; started_at:string;steps:string[]}[]};
type Doc = {id:string;kind:string;version:number;profile_version:string;fact_ids:string[];sha256:Record<string,string>};
type Operation = {id:string; kind:string; state:string; error:string|null;attempt_count:number;cancel_requested:boolean;updated_at:string};
type Source = {id:string;tenant:string;name:string;enabled:boolean;health_status:string;jobs_seen:number;last_error:string|null;last_success:string|null;backoff_until:string|null;poll_interval:number};
type Tab = 'Jobs'|'Profile'|'Sources'|'Applications'|'Activity';
const newFact = ():Fact => ({id:crypto.randomUUID(),kind:'skill',text:'',source:'',confidence:100,verified:false,user_approved:false});
const newProfile = ():Profile => ({id:crypto.randomUUID(),name:'',email:'',facts:[newFact()],answers:[]});
const stamp = (value:string|null|undefined) => value ? new Date(value).toLocaleString() : 'Not yet';

function App() {
  const [tab,setTab] = useState<Tab>('Jobs');
  const [profiles,setProfiles] = useState<Profile[]>([]);
  const [profileId,setProfileId] = useState(localStorage.getItem('mjobs-profile') || '');
  const [editor,setEditor] = useState<Profile>(newProfile);
  const [jobs,setJobs] = useState<Job[]>([]);
  const [apps,setApps] = useState<Application[]>([]);
  const [sources,setSources] = useState<Source[]>([]);
  const [ops,setOps] = useState<Operation[]>([]);
  const [ready,setReady] = useState<{status:string;checks:Record<string,boolean>}|null>(null);
  const [q,setQ] = useState(''); const [remote,setRemote]=useState(''); const [provider,setProvider]=useState(''); const [sort,setSort]=useState('score');
  const [detail,setDetail]=useState<Job|null>(null);
  const [selectedApp,setSelectedApp]=useState<Application|null>(null);
  const [docs,setDocs]=useState<Doc[]>([]);
  const [tenant,setTenant]=useState(''); const [mockConsent,setMockConsent]=useState(false);
  const [busy,setBusy]=useState(''); const [error,setError]=useState(''); const [notice,setNotice]=useState('');
  const [activeOp,setActiveOp]=useState('');
  const profileRef = useRef(profileId); profileRef.current = profileId;
  const queryRef = useRef({q,sort,remote,provider}); queryRef.current={q,sort,remote,provider};
  const jobsSequence = useRef(0); const detailSequence = useRef(0); const appSequence = useRef(0); const editorSequence = useRef(0);

  async function refresh(id=profileId) {
    const suffix = id ? `?profile_id=${encodeURIComponent(id)}` : '';
    const [p,a,s,o] = await Promise.all([api<{profiles:Profile[]}>('/profiles'),api<{applications:Application[]}>('/applications'+suffix),api<{sources:Source[]}>('/sources'),api<{operations:Operation[]}>('/operations'+suffix)]);
    setProfiles(p.profiles);setSources(s.sources);if(id===profileRef.current){setApps(a.applications);setOps(o.operations);}
  }
  async function loadJobs(id=profileId) {
    const sequence=++jobsSequence.current; const current=queryRef.current;
    const params = new URLSearchParams({q:current.q,sort:current.sort});
    if(id)params.set('profile_id',id);if(current.remote)params.set('remote_type',current.remote);if(current.provider)params.set('provider',current.provider);
    const result=await api<{jobs:Job[]}>('/jobs?'+params);
    if(sequence===jobsSequence.current&&id===profileRef.current)setJobs(result.jobs);
  }
  async function readiness() {
    try { const response=await fetch('/readiness');setReady(await response.json()); } catch {setReady({status:'unavailable',checks:{api:false}});}
  }
  useEffect(() => {void refresh().catch(e=>setError(String(e)));void readiness();const timer=setInterval(()=>void readiness(),15000);return()=>clearInterval(timer);},[]);
  useEffect(() => {const sequence=++editorSequence.current;++appSequence.current;localStorage.setItem('mjobs-profile',profileId);setDetail(null);setSelectedApp(null);setDocs([]);setApps([]);setOps([]);setMockConsent(false);void refresh(profileId).catch(e=>setError(String(e)));if(profileId)void api<Profile>('/profiles/'+encodeURIComponent(profileId)).then(p=>{if(sequence===editorSequence.current&&p.id===profileRef.current)setEditor({...p,facts:p.facts.map(f=>({...f,id:f.fact_id||f.id}))});}).catch(e=>setError(String(e)));},[profileId]);
  useEffect(() => {++jobsSequence.current;++detailSequence.current;setJobs([]);setDetail(null);const timer=setTimeout(()=>void loadJobs().catch(e=>setError(String(e))),250);return()=>clearTimeout(timer);},[q,sort,remote,provider,profileId]);
  async function action(label:string, fn:()=>Promise<void>) {
    if(busy)return;setBusy(label);setError('');setNotice('');
    try {await fn();await refresh(profileRef.current);await loadJobs(profileRef.current);}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy('');setActiveOp('');}
  }
  async function queue(path:string, body:unknown) {
    const result=await api<{operation_id:string;application_id?:string}>(path,{method:'POST',body:JSON.stringify(body)});
    setActiveOp(result.operation_id);
    const deadline=Date.now()+210000;
    while(Date.now()<deadline) {
      const operation=await api<Operation>('/operations/'+result.operation_id);
      setOps(current=>[operation,...current.filter(item=>item.id!==operation.id)]);
      if(terminal.has(operation.state)) {
        if(operation.state!=='COMPLETED')throw new Error(`${operation.state}: ${operation.error||'Review Activity for details'}`);
        setNotice('Completed. Your workspace has been updated.');return result;
      }
      await new Promise(resolve=>setTimeout(resolve,800));
    }
    throw new Error('This operation is still pending. Check Activity; it can recover when services return.');
  }
  const request = () => ({profile_id:profileId,request_id:crypto.randomUUID()});
  async function showJob(id:string) {
    const sequence=++detailSequence.current;const selected=profileRef.current;
    const result=await api<Job>('/jobs/'+id+(selected?'?profile_id='+encodeURIComponent(selected):''));
    if(sequence===detailSequence.current&&selected===profileRef.current)setDetail(result);
  }
  async function showApp(id:string) {
    const sequence=++appSequence.current;const selected=profileRef.current;
    const [application,documents]=await Promise.all([api<Application>('/applications/'+id),api<{documents:Doc[]}>('/applications/'+id+'/documents')]);
    if(sequence!==appSequence.current||selected!==profileRef.current||(selected&&application.profile_id!==selected))return;
    setSelectedApp(application);setDocs(documents.documents);setMockConsent(false);setTab('Applications');
  }
  async function demo() {
    const id='demo-'+crypto.randomUUID();
    const profile:Profile={id,name:'Fictional Test Applicant',email:'fixture@example.invalid',facts:[{id:'python',kind:'skill',text:'Python',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'sql',kind:'skill',text:'SQL',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}],answers:[{id:'country',text:'United States',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'remote',text:'yes',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'fixture_question',text:'Controlled fictional test application.',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}]};
    await api('/profiles',{method:'PUT',body:JSON.stringify(profile)});setProfileId(id);setEditor(profile);
    await queue('/discovery',{profile_id:id,request_id:crypto.randomUUID()});await refresh(id);await loadJobs(id);setTab('Jobs');setNotice('Fictional demo loaded. Mock applications stay inside this local test system.');
  }
  function editFact(index:number,change:Partial<Fact>) {setEditor(current=>({...current,facts:current.facts.map((fact,i)=>i===index?{...fact,...change}:fact)}));}
  return <div className="layout">
    <aside className="sidebar"><a className="brand" href="/">M<span>Jobs</span><small>YOUR LOCAL WORKSPACE</small></a><nav aria-label="Main navigation">{(['Jobs','Profile','Sources','Applications','Activity'] as Tab[]).map(item=><button key={item} aria-current={tab===item?'page':undefined} onClick={()=>setTab(item)}>{item}<span>{item==='Jobs'?jobs.length:item==='Applications'?apps.length:''}</span></button>)}</nav><div className="side-note"><strong>Built around your facts.</strong><p>Discover opportunities, prepare truthful documents, and keep a clear record.</p><span className="status">External submission OFF</span></div></aside>
    <main><header><div className="eyebrow">LOCAL FIRST · USER TESTING</div><div className="header-row"><h1>{tab==='Jobs'?'Find your next opportunity.':tab==='Profile'?'Your experience, in your words.':tab==='Sources'?'Choose where to look.':tab==='Applications'?'Every application, accounted for.':'See what is happening.'}</h1><span className={'status '+(ready?.status==='ready'?'good':'warn')}>{ready?.status==='ready'?'Services ready':'Services '+(ready?.status||'checking')}</span></div><div className="profile-switch"><label htmlFor="active-profile">Active profile</label><select id="active-profile" disabled={!!busy} value={profileId} onChange={e=>setProfileId(e.target.value)}><option value="">Select a profile</option>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="quiet" disabled={!!busy} onClick={()=>{setEditor(newProfile());setTab('Profile');}}>New profile</button></div></header>
      <div className="safety">Real applications require your own manual review and submission. This workspace cannot submit to an external employer.</div>
      {error&&<div role="alert" className="alert"><strong>Action needs attention</strong><p>{error}</p></div>}
      {notice&&<div role="status" className="notice">{notice}</div>}
      {busy&&<div role="status" className="notice">{busy}… {activeOp&&<button className="quiet" onClick={()=>void api('/operations/'+activeOp+'/cancel',{method:'POST'}).then(()=>setNotice('Cancellation requested.')).catch(e=>setError(String(e)))}>Cancel operation</button>}</div>}
      {tab==='Jobs'&&<>
        <div className="toolbar"><p>{jobs.length} stored opportunities <span className="muted">· matching uses approved facts</span></p><button disabled={!!busy||!profileId} onClick={()=>void action('Refreshing matches',async()=>{await queue('/matches',request());})}>Refresh matches</button></div>
        {!profileId&&<section className="welcome"><h2>Start with a profile.</h2><p>Add your own sourced career facts, or explore the complete flow with fictional demo data.</p><button className="primary" onClick={()=>setTab('Profile')}>Set up my profile</button> <button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>}
        <div className="filters"><label>Search jobs<input type="search" placeholder="Title, company or description" value={q} onChange={e=>setQ(e.target.value)}/></label><label>Work location<select aria-label="Work location" value={remote} onChange={e=>setRemote(e.target.value)}><option value="">All locations</option><option>REMOTE</option><option>HYBRID</option><option>ONSITE</option><option>UNKNOWN</option></select></label><label>Source<select aria-label="Source" value={provider} onChange={e=>setProvider(e.target.value)}><option value="">All sources</option><option value="greenhouse">Greenhouse</option><option value="mock-ats">Fictional mock</option></select></label><label>Sort<select aria-label="Sort" value={sort} onChange={e=>setSort(e.target.value)}><option value="score">Match score</option><option value="title">Title</option></select></label></div>
        <div className="job-grid"><div className="job-list">{jobs.map(job=><button className={'job-card '+(detail?.id===job.id?'selected':'')} key={job.id} onClick={()=>void showJob(job.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{job.company}</span><span className="score">{job.match_score===null?'Unscored':job.match_score+'%'}</span></div><h2>{job.title}</h2><p>{job.location} · {job.remote_type}</p><div className="card-foot"><span className={'tag '+(job.attributes.fictional?'fictional':'')}>{job.attributes.fictional?'Fictional demo':job.provider+' · read only'}</span><span>{job.posting_count} posting{job.posting_count===1?'':'s'}</span></div></button>)}{jobs.length===0&&<section className="empty"><h2>No matching jobs yet.</h2><p>Clear your filters, load a fictional demo, or configure a public job board in Sources.</p><button onClick={()=>setTab('Sources')}>Manage sources</button></section>}</div>
        <section className="job-detail">{detail?<><span className="eyebrow">{detail.company}</span><h2>{detail.title}</h2><p>{detail.location} · {detail.remote_type}</p><div className="divider"/><h3>Why this match?</h3>{detail.match?<><strong className="big-score">{detail.match.score}%</strong><p>{detail.match.components.method==='description_skill_mentions'?'Coverage of detected skill mentions in the description.':'Coverage of structured required skills in the fictional posting.'}</p><p><strong>Matched:</strong> {detail.match.components.matched?.join(', ')||'None'}</p><p><strong>Missing:</strong> {detail.match.missing.join(', ')||'None detected'}</p>{detail.match.components.limitation&&<p className="muted">{detail.match.components.limitation}</p>}</>:<p>Select a profile and refresh matches.</p>}
          {detail.attributes.salary&&<p>Published range: {detail.attributes.salary.map(s=>`${s.currency} ${s.min}–${s.max} (period ${s.period.toLowerCase()})`).join('; ')}</p>}
          <button className="primary" disabled={!!busy||!profileId} onClick={()=>void action('Preparing factual documents',async()=>{const op=await queue('/applications',{profile_id:profileId,job_id:detail.id,request_id:crypto.randomUUID()});if(op.application_id)await showApp(op.application_id);})}>Prepare documents</button>
          {!detail.attributes.fictional&&detail.application_url&&<a className="button-link" href={detail.application_url} target="_blank" rel="noreferrer">Open employer application ↗</a>}
          <h3>Job description</h3><p className="description">{detail.description||'No description supplied.'}</p><h3>Source evidence</h3>{detail.postings?.map(p=><div className="provenance" key={p.source+p.source_job_id}><code>{p.source}</code><p>Native posting {p.source_job_id} · fetched {stamp(p.fetched_at)}</p>{p.snapshots.map(s=><small key={s.sha256}>SHA256 {s.sha256}</small>)}</div>)}</>:<div className="empty"><h2>A closer look.</h2><p>Select an opportunity to see its description, source evidence, and match explanation.</p></div>}</section></div>
      </>}
      {tab==='Profile'&&<form onSubmit={e=>{e.preventDefault();void action('Saving profile',async()=>{const clean={id:editor.id,name:editor.name,email:editor.email,facts:editor.facts.map(({id,kind,text,source,confidence,verified,user_approved})=>({id,kind,text,source,confidence,verified,user_approved})),answers:editor.answers};await api('/profiles',{method:'PUT',body:JSON.stringify(clean)});setProfileId(editor.id);await queue('/matches',{profile_id:editor.id,request_id:crypto.randomUUID()});setNotice('Profile saved. Pending facts remain excluded from documents and matching.');});}}>
        <section className="panel"><h2>Contact details</h2><div className="two-col"><label>Name<input required maxLength={200} value={editor.name} onChange={e=>setEditor({...editor,name:e.target.value})}/></label><label>Email<input type="email" required maxLength={320} value={editor.email} onChange={e=>setEditor({...editor,email:e.target.value})}/></label></div></section>
        <section className="panel"><div className="toolbar"><div><h2>Career facts</h2><p>Use one skill or factual statement per item. Add evidence and approve it explicitly.</p></div><button type="button" onClick={()=>setEditor({...editor,facts:[...editor.facts,newFact()]})}>Add fact</button></div>{editor.facts.map((fact,i)=><fieldset className="fact" key={fact.id}><legend>Fact {i+1} <span className="tag">{eligible(fact)?'Eligible':'Pending'}</span></legend><div className="two-col"><label>Type<select value={fact.kind} onChange={e=>editFact(i,{kind:e.target.value})}>{['skill','experience','education','certification','project','accomplishment'].map(k=><option key={k}>{k}</option>)}</select></label><label>Confidence (0–100)<input type="number" min={0} max={100} required value={fact.confidence} onChange={e=>editFact(i,{confidence:Number(e.target.value)})}/></label></div><label>Fact text<textarea required maxLength={10000} value={fact.text} onChange={e=>editFact(i,{text:e.target.value})}/></label><label>Evidence / source<input required maxLength={1000} value={fact.source} onChange={e=>editFact(i,{source:e.target.value})}/></label><div className="approvals"><label><input type="checkbox" checked={fact.verified} onChange={e=>editFact(i,{verified:e.target.checked})}/> I verified this fact</label><label><input type="checkbox" checked={fact.user_approved} onChange={e=>editFact(i,{user_approved:e.target.checked})}/> I approve its use in documents</label><button type="button" className="quiet" disabled={editor.facts.length===1} onClick={()=>setEditor({...editor,facts:editor.facts.filter((_,index)=>index!==i)})}>Remove</button></div></fieldset>)}<p className="muted">Only sourced, verified, approved facts with confidence of at least 80 are included. Existing application documents retain their original profile snapshot.</p><button className="primary" disabled={!!busy}>Save profile</button></section>
        <section className="panel"><h2>Try the local demo</h2><p>Creates a separate fictional profile and two fictional jobs. It never overwrites your profile.</p><button type="button" disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>
      </form>}
      {tab==='Sources'&&<><section className="panel"><h2>Public Greenhouse board</h2><p>Reads public job listings from the provider API. Enter the board token from an employer’s Greenhouse careers URL. No login or application submission is performed.</p><form onSubmit={e=>{e.preventDefault();void action('Saving source',async()=>{await api('/sources',{method:'PUT',body:JSON.stringify({tenant,enabled:true,poll_interval:300})});setTenant('');setNotice('Source saved. Select a profile, then collect jobs.');});}}><div className="source-add"><label>Board token<input required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,79}" value={tenant} onChange={e=>setTenant(e.target.value)} placeholder="e.g. greenhouse"/></label><button disabled={!!busy}>Add board</button></div></form></section>{sources.map(source=><section className="panel" key={source.id}><div className="toolbar"><div><h2>{source.name}</h2><p>greenhouse / {source.tenant} · {source.jobs_seen} jobs collected</p></div><span className="tag">{source.enabled?source.health_status:'DISABLED'}</span></div><p>Last success: {stamp(source.last_success)} · Minimum interval: {source.poll_interval/60} minutes</p>{source.backoff_until&&<p className="alert">Backoff until {stamp(source.backoff_until)}: {source.last_error}</p>}<button disabled={!!busy||!profileId||!source.enabled} onClick={()=>void action('Collecting public jobs',async()=>{await queue('/sources/'+source.id+'/collect',request());setTab('Jobs');})}>Collect jobs</button> <button className="quiet" disabled={!!busy} onClick={()=>void action('Updating source',async()=>{await api('/sources',{method:'PUT',body:JSON.stringify({tenant:source.tenant,enabled:!source.enabled,poll_interval:source.poll_interval})});})}>{source.enabled?'Disable':'Enable'}</button></section>)}<section className="panel"><h2>Fictional local source</h2><p>Use the demo to test document preparation, dry-run, mock confirmation and tracking without contacting an employer.</p><button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section></>}
      {tab==='Applications'&&<div className="job-grid"><section><h2>{apps.length} prepared applications</h2>{apps.map(application=><button className="job-card" key={application.id} onClick={()=>void showApp(application.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{application.company}</span><span className="tag">{application.target==='MOCK'?'Fictional mock':'Manual only'}</span></div><h2>{application.title}</h2><p>{application.state} · documents v{application.document_version}</p></button>)}{!apps.length&&<p className="empty">Select a job and prepare documents to start tracking it here.</p>}</section><section className="job-detail">{selectedApp?<><h2>Application details</h2><span className="tag">{selectedApp.state}</span><p>Document revision {selectedApp.document_version} · {selectedApp.fact_ids.length} approved facts</p><h3>Documents</h3>{docs.map(document=><div className="document" key={document.id}><strong>{document.kind} · v{document.version}</strong><p>{document.fact_ids.length} factual sources · profile snapshot {document.profile_version.slice(0,10)}</p><a className="button-link" href={'/documents/'+document.id+'/pdf'}>Download PDF</a><a className="button-link" href={'/documents/'+document.id+'/docx'}>Download DOCX</a></div>)}
        {selectedApp.target==='MOCK'?<section className="mock-box"><h3>Controlled fictional test</h3><p>The dry-run fills and reviews the local mock form, then stops before submission.</p><button disabled={!!busy||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Running mock dry-run',async()=>{await queue('/applications/'+selectedApp.id+'/mock-run',{submit:false});await showApp(selectedApp.id);})}>Run dry-run</button><label className="consent"><input type="checkbox" checked={mockConsent} onChange={e=>setMockConsent(e.target.checked)}/> I approve one fictional mock submission</label><button disabled={!!busy||!mockConsent||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Submitting to local mock',async()=>{await queue('/applications/'+selectedApp.id+'/mock-run',{submit:true});await showApp(selectedApp.id);})}>Submit to local mock</button></section>:<p className="safety">Manual preparation only. Review the documents and use the employer’s own application page.</p>}
        <h3>Tracking evidence</h3><p className="description">{selectedApp.trace.join(' → ')}</p>{selectedApp.confirmation&&<pre>{JSON.stringify(selectedApp.confirmation,null,2)}</pre>}<h3>Attempts</h3>{selectedApp.attempts?.map((attempt,i)=><p key={i}>{stamp(attempt.started_at)} · {attempt.state} · {attempt.steps.join(' → ')}</p>)}</>:<div className="empty"><h2>Your paper trail.</h2><p>Select an application to download documents and inspect its status and attempts.</p></div>}</section></div>}
      {tab==='Activity'&&<><section className="panel"><div className="toolbar"><h2>Service health</h2><button onClick={()=>{void readiness();void refresh().catch(e=>setError(String(e)));}}>Refresh activity</button></div>{ready&&Object.entries(ready.checks).map(([name,ok])=><p key={name}><span className={'status '+(ok?'good':'warn')}>{ok?'Ready':'Unavailable'}</span> {name}</p>)}<p className="muted">Queued work is durable. A browser interruption before submission requires review; uncertain submission outcomes require reconciliation.</p></section><section className="panel"><h2>Background operations</h2>{ops.map(op=><div className="operation" key={op.id}><div><strong>{op.kind}</strong><p>{op.state} · {op.attempt_count} attempt(s) · {stamp(op.updated_at)}</p>{op.error&&<p className="error-text">{op.error}</p>}<code>{op.id.slice(0,16)}</code></div>{!terminal.has(op.state)&&<button onClick={()=>void api('/operations/'+op.id+'/cancel',{method:'POST'}).then(()=>refresh()).catch(e=>setError(String(e)))}>{op.cancel_requested?'Cancellation requested':'Cancel'}</button>}</div>)}{!ops.length&&<p>No operations for this profile yet.</p>}</section></>}
      <footer>MJobs · Your data stays in this local workspace. User-testing milestone; broader integrations and automation are still in development.</footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
