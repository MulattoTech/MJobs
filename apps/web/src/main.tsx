import React, { useEffect, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { api, eligible, terminal } from './api';
import { CompanySources, type CompanySourceView } from './company-sources';
import { CompanyDiscovery } from './company-discovery';
import { CompanyDiscoveryRequests, type DiscoveryRequest } from './company-discovery-requests';
import { addSelectedExtraction, cleanLines } from './profile-import';
import { ResumeLibrary, type Resume, type Extraction } from './resume-library';
import { SourceValueEvidence, type SourceValueResolution } from './source-value-evidence';
import { DocumentHistory, type GeneratedDocument } from './document-history';
import { DocumentLibrary } from './document-library';
import { ResumeFactSelection,type ResumeFactChoice } from './resume-fact-selection';
import './style.css';

type Fact = { id: string; fact_id?: string; kind: string; text: string; source: string; confidence: number; verified: boolean; user_approved: boolean };
type Answer = Omit<Fact, 'kind' | 'fact_id'>;
type Contact = {first_name:string;middle_name:string;last_name:string;preferred_name:string;phones:string[];address_line1:string;address_line2:string;city:string;state:string;postal_code:string;country:string;linkedin:string;github:string;portfolio:string};
type Preferences = {desired_roles:string[];employment_types:string[];work_arrangements:string[];shifts:string[];max_commute_miles:number|null;max_commute_minutes:number|null;travel_preference:string;salary_expectations:string;career_goals:string;work_authorization:string;sponsorship_needs:string};
type Profile = {id:string; name:string; email:string; version?:string; facts:Fact[]; answers:Answer[]; contact?:Partial<Contact>; preferences?:Partial<Preferences>};
type Match = {score:number; missing:string[]; components:{method?:string; matched?:string[]; detected?:string[]; limitation?:string}};
type Job = {id:string; title:string; company:string; provider:string; location:string; remote_type:string; description:string; skills:string[]; application_url:string; match_score:number|null; posting_count:number; attributes:{fictional?:boolean; salary?:{min:number;max:number;currency:string;period:string}[]; last_collected_at?:string; source_resolution?:SourceValueResolution|null}; match?:Match|null; postings?:{source:string;source_job_id:string;fetched_at:string;snapshots:{sha256:string;retrieved_at:string}[]}[]};
type Application = {id:string; title:string; company:string; state:string; target:string; job_id:string; profile_id:string; document_version:number; profile_version:string; fact_ids:string[]; trace:string[]; confirmation:Record<string,unknown>|null; attempts?:{state:string; started_at:string;steps:string[]}[]};
type Doc = GeneratedDocument;
type Operation = {id:string; kind:string; state:string; error:string|null;attempt_count:number;cancel_requested:boolean;updated_at:string};
type Source = {id:string;provider:string;tenant:string;name:string;enabled:boolean;health_status:string;jobs_seen:number;last_error:string|null;last_success:string|null;backoff_until:string|null;poll_interval:number;concurrency:number;request_timeout:number;rate_limit:number;jitter_seconds:number;strategy:string;capabilities:{capabilities:string[]}};
type SearchProfile = {id:string;profile_id:string;name:string;enabled:boolean;criteria:{include_titles:string[];remote:string;employment_types:string[]};source_ids:string[];created_at:string;updated_at:string};
type OperationMode = 'FIXTURE'|'PERSONAL';
type Tab = 'Jobs'|'Profile'|'Sources'|'Applications'|'Activity';
const newFact = ():Fact => ({id:crypto.randomUUID(),kind:'skill',text:'',source:'',confidence:100,verified:false,user_approved:false});
const newProfile = ():Profile => ({id:crypto.randomUUID(),name:'',email:'',facts:[],answers:[],contact:{},preferences:{}});
const stamp = (value:string|null|undefined) => value ? new Date(value).toLocaleString() : 'Not yet';

function App() {
  const [tab,setTab] = useState<Tab>('Jobs');
  const [profiles,setProfiles] = useState<Profile[]>([]);
  const [profileId,setProfileId] = useState(localStorage.getItem('mjobs-profile') || '');
  const [editor,setEditor] = useState<Profile>(newProfile);
  const [jobs,setJobs] = useState<Job[]>([]);
  const [apps,setApps] = useState<Application[]>([]);
  const [sources,setSources] = useState<Source[]>([]);
  const [companySources,setCompanySources] = useState<CompanySourceView[]>([]);
  const [discoveryRequests,setDiscoveryRequests] = useState<DiscoveryRequest[]>([]);
  const companySourcesSequence = useRef(0);
  const [operationMode,setOperationMode] = useState<OperationMode|null>(null);
  const [ops,setOps] = useState<Operation[]>([]);
  const [ready,setReady] = useState<{status:string;checks:Record<string,boolean>}|null>(null);
  const [q,setQ] = useState(''); const [remote,setRemote]=useState(''); const [provider,setProvider]=useState(''); const [sort,setSort]=useState('score');
  const [detail,setDetail]=useState<Job|null>(null);
  const [tailorResume,setTailorResume]=useState(false);
  const [resumeSelection,setResumeSelection]=useState<ResumeFactChoice|null>(null);
  useEffect(()=>{setTailorResume(false);setResumeSelection(null);},[detail?.id,profileId]);
  const [selectedApp,setSelectedApp]=useState<Application|null>(null);
  const [docs,setDocs]=useState<Doc[]>([]);
  const [resumes,setResumes]=useState<Resume[]>([]);
  const [tenant,setTenant]=useState(''); const [sourceIntervalMinutes,setSourceIntervalMinutes]=useState(30); const [sourceConcurrency,setSourceConcurrency]=useState(1); const [sourceTimeout,setSourceTimeout]=useState(10); const [sourceRate,setSourceRate]=useState(60); const [sourceJitter,setSourceJitter]=useState(30);
  const [searchProfiles,setSearchProfiles]=useState<SearchProfile[]>([]); const [searchName,setSearchName]=useState(''); const [searchTitles,setSearchTitles]=useState(''); const [searchRemote,setSearchRemote]=useState<'ANY'|'REQUIRED'|'EXCLUDED'>('ANY'); const [searchEmployment,setSearchEmployment]=useState('FULL_TIME'); const [searchSource,setSearchSource]=useState('');
  const [mockConsent,setMockConsent]=useState(false);
  const [busy,setBusy]=useState(''); const [error,setError]=useState(''); const [notice,setNotice]=useState('');
  const [activeOp,setActiveOp]=useState('');
  const profileRef = useRef(profileId); profileRef.current = profileId;
  const queryRef = useRef({q,sort,remote,provider}); queryRef.current={q,sort,remote,provider};
  const jobsSequence = useRef(0); const detailSequence = useRef(0); const appSequence = useRef(0); const editorSequence = useRef(0);

  async function refresh(id=profileId) {
    const companySequence = ++companySourcesSequence.current;
    const suffix = id ? `?profile_id=${encodeURIComponent(id)}` : '';
    const [p,a,s,o,sp,cs,dr] = await Promise.all([api<{profiles:Profile[]}>('/profiles'),api<{applications:Application[]}>('/applications'+suffix),api<{sources:Source[]}>('/sources'),api<{operations:Operation[]}>('/operations'+suffix),id?api<{search_profiles:SearchProfile[]}>('/search-profiles'+suffix):Promise.resolve({search_profiles:[]} as {search_profiles:SearchProfile[]}),api<{company_sources:CompanySourceView[]}>('/company-sources/view'+suffix),api<{requests:DiscoveryRequest[]}>('/company-discovery/requests')]);
    if(companySequence===companySourcesSequence.current&&id===profileRef.current){setCompanySources(cs.company_sources);setDiscoveryRequests(dr.requests);}
    setProfiles(p.profiles);setSources(s.sources);if(id===profileRef.current){setApps(a.applications);setOps(o.operations);setSearchProfiles(sp.search_profiles);}
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
  useEffect(() => {void refresh().catch(e=>setError(String(e)));void api<{operation_mode:OperationMode}>('/settings').then(s=>setOperationMode(s.operation_mode)).catch(e=>setError(String(e)));void readiness();const timer=setInterval(()=>void readiness(),15000);return()=>clearInterval(timer);},[]);
  useEffect(() => {const sequence=++editorSequence.current;++appSequence.current;localStorage.setItem('mjobs-profile',profileId);setDetail(null);setSelectedApp(null);setDocs([]);setResumes([]);setApps([]);setOps([]);setCompanySources([]);setMockConsent(false);void refresh(profileId).catch(e=>setError(String(e)));if(profileId){void api<Profile>('/profiles/'+encodeURIComponent(profileId)).then(p=>{if(sequence===editorSequence.current&&p.id===profileRef.current)setEditor({...p,contact:p.contact||{},preferences:p.preferences||{},facts:(p.facts||[]).map(f=>({...f,id:f.fact_id||f.id}))});}).catch(e=>setError(String(e)));void api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(profileId)+'/resumes').then(r=>{if(sequence===editorSequence.current&&profileId===profileRef.current)setResumes(r.resumes);}).catch(e=>setError(String(e)));}},[profileId]);
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
  async function discoverCompany(companyName:string, companyWebsite:string) {
    const result = await api<{status:string;operation_id:string|null}>('/companies/discovery', {
      method:'POST', body:JSON.stringify({company_name:companyName,company_website:companyWebsite,
        reason:'USER_ADDED_COMPANY',request_id:crypto.randomUUID(),...(profileId?{profile_id:profileId}:{})})
    });
    if (result.status === 'ALREADY_MONITORED') {
      setNotice('This company is already being monitored. Its saved source information is unchanged.');
      return;
    }
    if (!result.operation_id) throw new Error('Discovery could not be queued. Check Activity for details.');
    setActiveOp(result.operation_id);
    const deadline=Date.now()+210000;
    while(Date.now()<deadline) {
      const operation=await api<Operation>('/operations/'+result.operation_id);
      setOps(current=>[operation,...current.filter(item=>item.id!==operation.id)]);
      if(terminal.has(operation.state)) {
        if(operation.state!=='COMPLETED') throw new Error(`${operation.state}: ${operation.error||'Review Activity for details'}`);
        setNotice('Completed. Public company jobs and source information have been updated.');
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,800));
    }
    throw new Error('This discovery is still pending. Check Activity; it can recover when services return.');
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
  async function prepareJobRoute(job:Job, routeMode:'DEFAULT'|'THIS_ROUTE'='DEFAULT', routeId?:string) {
    if(tailorResume&&resumeSelection&&(!resumeSelection.version||!resumeSelection.ids.length||resumeSelection.profileId!==profileId||resumeSelection.jobId!==job.id))throw new Error('Reload approved facts and select at least one fact for this resume.');
    const op=await queue('/applications',{profile_id:profileId,job_id:job.id,request_id:crypto.randomUUID(),route_mode:routeMode,tailor_resume:tailorResume,
      ...(tailorResume&&resumeSelection?{tailor_fact_ids:resumeSelection.ids,expected_profile_version:resumeSelection.version}:{}),...(routeId?{route_id:routeId}:{})});
    if(op.application_id)await showApp(op.application_id);
  }
  async function demo() {
    const id='demo-'+crypto.randomUUID();
    const profile:Profile={id,name:'Fictional Test Applicant',email:'fixture@example.invalid',facts:[{id:'python',kind:'skill',text:'Python',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'sql',kind:'skill',text:'SQL',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}],answers:[{id:'country',text:'United States',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'remote',text:'yes',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'fixture_question',text:'Controlled fictional test application.',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}]};
    await api('/profiles',{method:'PUT',body:JSON.stringify(profile)});setProfileId(id);setEditor(profile);
    await queue('/discovery',{profile_id:id,request_id:crypto.randomUUID()});await refresh(id);await loadJobs(id);setTab('Jobs');setNotice('Fictional demo loaded. Mock applications stay inside this local test system.');
  }
  function editFact(index:number,change:Partial<Fact>) {setEditor(current=>({...current,facts:current.facts.map((fact,i)=>i===index?{...fact,...change}:fact)}));}
  function setContact(key:keyof Contact,value:string) { setEditor(current=>({...current,contact:{...current.contact,[key]:value}})); }
  function setPreference(key:keyof Preferences,value:unknown) { setEditor(current=>({...current,preferences:{...current.preferences,[key]:value}})); }
  function addExtraction(extraction:Extraction, selectedFacts:Set<string>, selectedContact:Set<string>) {
    const result = addSelectedExtraction({name:editor.name,email:editor.email,contact:editor.contact as Record<string,unknown>,facts:editor.facts.filter(f=>f.text.trim())}, extraction, selectedFacts, selectedContact);
    setEditor(current=>({...current,name:result.profile.name,email:result.profile.email,contact:result.profile.contact as Partial<Contact>,facts:result.profile.facts as Fact[]}));
    setNotice(result.truncated?'Profile has reached 200 facts. Some suggestions were not added. Review existing facts before importing more.':`${result.added} pending facts added; ${result.skipped} existing suggestions skipped. Review, verify and approve before saving.`);
  }
  return <div className="layout" data-operation-mode={operationMode||'LOADING'}>
    <aside className="sidebar"><a className="brand" href="/">M<span>Jobs</span><small>YOUR LOCAL WORKSPACE</small></a><nav aria-label="Main navigation">{(['Jobs','Profile','Sources','Applications','Activity'] as Tab[]).map(item=><button key={item} aria-current={tab===item?'page':undefined} onClick={()=>setTab(item)}>{item}<span>{item==='Jobs'?jobs.length:item==='Applications'?apps.length:''}</span></button>)}</nav><div className="side-note"><strong>Built around your facts.</strong><p>Discover opportunities, prepare truthful documents, and keep a clear record.</p><span className="status">External submission OFF</span></div></aside>
    <main><header><div className="eyebrow">LOCAL FIRST · USER TESTING</div><div className="header-row"><h1>{tab==='Jobs'?'Find your next opportunity.':tab==='Profile'?'Your experience, in your words.':tab==='Sources'?'Choose where to look.':tab==='Applications'?'Every application, accounted for.':'See what is happening.'}</h1><span className={'status '+(ready?.status==='ready'?'good':'warn')}>{ready?.status==='ready'?'Services ready':'Services '+(ready?.status||'checking')}</span></div><div className="profile-switch"><label htmlFor="active-profile">Active profile</label><select id="active-profile" disabled={!!busy} value={profileId} onChange={e=>setProfileId(e.target.value)}><option value="">Select a profile</option>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="quiet" disabled={!!busy} onClick={()=>{++editorSequence.current;setProfileId('');setEditor(newProfile());setTab('Profile');}}>New profile</button></div></header>
      <div className="safety">Real applications require your own manual review and submission. This workspace cannot submit to an external employer.</div>
      {error&&<div role="alert" className="alert"><strong>Action needs attention</strong><p>{error}</p></div>}
      {notice&&<div role="status" className="notice">{notice}</div>}
      {busy&&<div role="status" className="notice">{busy}… {activeOp&&<button className="quiet" onClick={()=>void api('/operations/'+activeOp+'/cancel',{method:'POST'}).then(()=>setNotice('Cancellation requested.')).catch(e=>setError(String(e)))}>Cancel operation</button>}</div>}
      {tab==='Jobs'&&<>
        <div className="toolbar"><p>{jobs.length} stored opportunities <span className="muted">· matching uses approved facts</span></p><button disabled={!!busy||!profileId} onClick={()=>void action('Refreshing matches',async()=>{await queue('/matches',request());})}>Refresh matches</button></div>
        {!profileId&&<section className="welcome"><h2>Start with a profile.</h2><p>{operationMode==='FIXTURE'?'Add your own sourced career facts, or explore the flow with fictional demo data.':'Add your own sourced career facts to begin.'}</p><button className="primary" onClick={()=>setTab('Profile')}>Set up my profile</button> {operationMode==='FIXTURE'&&<button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button>}</section>}
        <div className="filters"><label>Search jobs<input type="search" placeholder="Title, company or description" value={q} onChange={e=>setQ(e.target.value)}/></label><label>Work location<select aria-label="Work location" value={remote} onChange={e=>setRemote(e.target.value)}><option value="">All locations</option><option>REMOTE</option><option>HYBRID</option><option>ONSITE</option><option>UNKNOWN</option></select></label><label>Source<select aria-label="Source" value={provider} onChange={e=>setProvider(e.target.value)}><option value="">All sources</option><option value="greenhouse">Greenhouse</option><option value="mock-ats">Fictional mock</option></select></label><label>Sort<select aria-label="Sort" value={sort} onChange={e=>setSort(e.target.value)}><option value="score">Match score</option><option value="title">Title</option></select></label></div>
        <div className="job-grid"><div className="job-list">{jobs.map(job=><button className={'job-card '+(detail?.id===job.id?'selected':'')} key={job.id} onClick={()=>void showJob(job.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{job.company}</span><span className="score">{job.match_score===null?'Unscored':job.match_score+'%'}</span></div><h2>{job.title}</h2><p>{job.location} · {job.remote_type}</p><div className="card-foot"><span className={'tag '+(job.attributes.fictional?'fictional':'')}>{job.attributes.fictional?'Fictional demo':job.provider+' · read only'}</span><span>{job.posting_count} posting{job.posting_count===1?'':'s'}</span></div></button>)}{jobs.length===0&&<section className="empty"><h2>No matching jobs yet.</h2><p>{operationMode==='FIXTURE'?'Clear your filters, load a fictional demo, or configure a public job board in Sources.':'Clear your filters or configure a public job board in Sources.'}</p><button onClick={()=>setTab('Sources')}>Manage sources</button></section>}</div>
        <section className="job-detail">{detail?<><span className="eyebrow">{detail.company}</span><h2>{detail.title}</h2><p>{detail.location} · {detail.remote_type}</p><div className="divider"/><h3>Why this match?</h3>{detail.match?<><strong className="big-score">{detail.match.score}%</strong><p>{detail.match.components.method==='description_skill_mentions'?'Coverage of detected skill mentions in the description.':'Coverage of structured required skills in the fictional posting.'}</p><p><strong>Matched:</strong> {detail.match.components.matched?.join(', ')||'None'}</p><p><strong>Missing:</strong> {detail.match.missing.join(', ')||'None detected'}</p>{detail.match.components.limitation&&<p className="muted">{detail.match.components.limitation}</p>}</>:<p>Select a profile and refresh matches.</p>}
          {detail.attributes.salary&&<p>Published range: {detail.attributes.salary.map(s=>`${s.currency} ${s.min}–${s.max} (period ${s.period.toLowerCase()})`).join('; ')}</p>}
          <label className="consent"><input type="checkbox" checked={tailorResume} disabled={!!busy||!profileId} onChange={e=>{setTailorResume(e.target.checked);setResumeSelection(null);}}/> Order approved skills for this job</label><p className="muted">Skills are ordered within the chosen resume facts. Job-matched skills and technology facts appear in bold in DOCX and PDF. Approved wording is preserved. Review the generated document before use.</p>
          {tailorResume&&<ResumeFactSelection key={profileId+':'+detail.id} profileId={profileId} jobId={detail.id} disabled={!!busy} onChange={setResumeSelection}/>}
          <SourceValueEvidence resolution={detail.attributes.source_resolution} onPreparePreserved={!busy&&!!profileId&&detail.attributes.source_resolution?.route_review_required&&detail.attributes.source_resolution.preserved_route_id?()=>void action('Preparing with preserved route',()=>prepareJobRoute(detail,'THIS_ROUTE',detail.attributes.source_resolution?.preserved_route_id||undefined)):undefined}/>
          <button className="primary" disabled={!!busy||!profileId||!!detail.attributes.source_resolution?.route_review_required} onClick={()=>void action('Preparing factual documents',()=>prepareJobRoute(detail))}>Prepare documents</button>
          {!detail.attributes.fictional&&detail.application_url&&<a className="button-link" href={detail.application_url} target="_blank" rel="noreferrer">Open employer application ↗</a>}
          <h3>Job description</h3><p className="description">{detail.description||'No description supplied.'}</p><h3>Source evidence</h3>{detail.postings?.map(p=><div className="provenance" key={p.source+p.source_job_id}><code>{p.source}</code><p>Native posting {p.source_job_id} · fetched {stamp(p.fetched_at)}</p>{p.snapshots.map(s=><small key={s.sha256}>SHA256 {s.sha256}</small>)}</div>)}</>:<div className="empty"><h2>A closer look.</h2><p>Select an opportunity to see its description, source evidence, and match explanation.</p></div>}</section></div>
      </>}
      {tab==='Profile'&&<form onSubmit={e=>{e.preventDefault();void action('Saving profile',async()=>{const clean={id:editor.id,name:editor.name,email:editor.email,contact:{...editor.contact,phones:cleanLines(editor.contact?.phones||[])},preferences:{...editor.preferences,desired_roles:cleanLines(editor.preferences?.desired_roles||[])},facts:editor.facts.filter(f=>f.text.trim()).map(({id,kind,text,source,confidence,verified,user_approved})=>({id,kind,text,source,confidence,verified,user_approved})),answers:editor.answers};await api('/profiles',{method:'PUT',body:JSON.stringify(clean)});setProfileId(editor.id);if(clean.facts.some(eligible))await queue('/matches',{profile_id:editor.id,request_id:crypto.randomUUID()});if(editor.facts.some(f=>!f.text.trim()))setNotice('Profile saved. Empty fact rows were ignored.');else setNotice('Profile saved. Pending facts remain excluded from documents and matching.');});}}>
        <section className="panel"><h2>Contact details</h2><p className="muted">Name and email are required to save. Other fields are optional and stay in this profile.</p><div className="two-col"><label>Full name<input required maxLength={200} value={editor.name} onChange={e=>setEditor({...editor,name:e.target.value})}/></label><label>Email<input type="email" required maxLength={320} value={editor.email} onChange={e=>setEditor({...editor,email:e.target.value})}/></label></div><div className="two-col">{(['first_name','middle_name','last_name','preferred_name'] as const).map(key=><label key={key}>{key.replaceAll('_',' ')}<input value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div><label>Phone numbers (one per line)<textarea value={(editor.contact?.phones||[]).join('\n')} onChange={e=>setEditor({...editor,contact:{...editor.contact,phones:e.target.value.split('\n')}})}/></label><div className="two-col">{(['address_line1','address_line2','city','state','postal_code','country'] as const).map(key=><label key={key}>{key.replaceAll('_',' ')}<input value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div><div className="two-col">{(['linkedin','github','portfolio'] as const).map(key=><label key={key}>{key}<input type="url" value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div></section>
        <section className="panel"><h2>Preferences</h2><p className="muted">Stored preferences are ready for future matching controls; they are not automatic search filters yet.</p><label>Desired roles (one per line)<textarea value={(editor.preferences?.desired_roles||[]).join('\n')} onChange={e=>setPreference('desired_roles',e.target.value.split('\n'))}/></label><div className="two-col"><label>Employment types<select multiple value={editor.preferences?.employment_types||[]} onChange={e=>setPreference('employment_types',Array.from(e.target.selectedOptions,e=>e.value))}>{['FULL_TIME','PART_TIME','CONTRACT','CONSULTING','PER_DIEM','TEMPORARY','INTERNSHIP'].map(v=><option key={v}>{v}</option>)}</select></label><label>Work arrangements<select multiple value={editor.preferences?.work_arrangements||[]} onChange={e=>setPreference('work_arrangements',Array.from(e.target.selectedOptions,e=>e.value))}>{['REMOTE','HYBRID','ON_SITE'].map(v=><option key={v}>{v}</option>)}</select></label></div><div className="two-col"><label>Shifts<select multiple value={editor.preferences?.shifts||[]} onChange={e=>setPreference('shifts',Array.from(e.target.selectedOptions,e=>e.value))}>{['DAY','EVENING','NIGHT','FLEXIBLE'].map(v=><option key={v}>{v}</option>)}</select></label><label>Travel preference<input value={editor.preferences?.travel_preference||''} onChange={e=>setPreference('travel_preference',e.target.value)}/></label></div><div className="two-col"><label>Salary expectations<input value={editor.preferences?.salary_expectations||''} onChange={e=>setPreference('salary_expectations',e.target.value)}/></label><label>Career goals<textarea value={editor.preferences?.career_goals||''} onChange={e=>setPreference('career_goals',e.target.value)}/></label></div><div className="two-col"><label>Maximum commute miles<input type="number" min="0" value={editor.preferences?.max_commute_miles??''} onChange={e=>setPreference('max_commute_miles',e.target.value===''?null:Number(e.target.value))}/></label><label>Maximum commute minutes<input type="number" min="0" value={editor.preferences?.max_commute_minutes??''} onChange={e=>setPreference('max_commute_minutes',e.target.value===''?null:Number(e.target.value))}/></label></div></section>
        <section className="panel"><h2>Work authorization</h2><p>Optional, in your own words. These details do not approve application answers or authorize submission.</p><div className="two-col"><label>Work authorization details<input maxLength={300} value={editor.preferences?.work_authorization||''} onChange={e=>setPreference('work_authorization',e.target.value)}/></label><label>Sponsorship needs<select value={editor.preferences?.sponsorship_needs||'NOT_SPECIFIED'} onChange={e=>setPreference('sponsorship_needs',e.target.value)}><option value="NOT_SPECIFIED">Not specified</option><option value="REQUIRED">Sponsorship required</option><option value="NOT_REQUIRED">Sponsorship not required</option></select></label></div></section>
        <section className="panel"><div className="toolbar"><div><h2>Career facts</h2><p>Facts imported from a resume stay unverified until you check both boxes.</p></div><button type="button" onClick={()=>setEditor({...editor,facts:[...editor.facts,newFact()]})}>Add fact</button></div>{editor.facts.map((fact,i)=><fieldset className="fact" key={fact.id}><legend>Fact {i+1} <span className="tag">{eligible(fact)?'Eligible':'Pending'}</span></legend><div className="two-col"><label>Type<select value={fact.kind} onChange={e=>editFact(i,{kind:e.target.value})}>{['skill','summary','experience','responsibility','education','certification','project','accomplishment','technology_experience'].map(k=><option key={k}>{k}</option>)}</select></label><label>Confidence (0–100)<input type="number" min={0} max={100} required value={fact.confidence} onChange={e=>editFact(i,{confidence:Number(e.target.value)})}/></label></div><label>Fact text<textarea maxLength={10000} value={fact.text} onChange={e=>editFact(i,{text:e.target.value})}/></label><label>Evidence / source<input maxLength={1000} value={fact.source} onChange={e=>editFact(i,{source:e.target.value})}/></label><div className="approvals"><label><input type="checkbox" checked={fact.verified} onChange={e=>editFact(i,{verified:e.target.checked})}/> I verified this fact</label><label><input type="checkbox" checked={fact.user_approved} onChange={e=>editFact(i,{user_approved:e.target.checked})}/> I approve its use in documents</label><button type="button" className="quiet" onClick={()=>setEditor({...editor,facts:editor.facts.filter((_,index)=>index!==i)})}>Remove</button></div></fieldset>)}<p className="muted">Only sourced, verified, approved facts with confidence of at least 80 are included.</p><button className="primary" disabled={!!busy}>Save profile</button></section>
        <ResumeLibrary key={editor.id} profileId={editor.id} saved={profileId===editor.id&&profiles.some(p=>p.id===editor.id)} resumes={editor.id===profileId?resumes:[]} busy={!!busy} action={action} onStored={rows=>{setResumes(rows);setNotice('Resume stored locally. Use Extract and review to populate your profile.');}} onApply={addExtraction}/>
        <DocumentLibrary key={'library-'+editor.id} profileId={editor.id} saved={profileId===editor.id&&profiles.some(p=>p.id===editor.id)} baseResumes={resumes} busy={!!busy} action={action} onBaseStored={async()=>{const rows=await api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(editor.id)+'/resumes');if(editor.id===profileRef.current)setResumes(rows.resumes);}}/>
        {operationMode==='FIXTURE'&&<section className="panel"><h2>Try the local demo</h2><p>Creates a separate fictional profile and two fictional jobs. It never overwrites your profile.</p><button type="button" disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>}
      </form>}
      {tab==='Sources'&&<>
        <CompanySources rows={companySources} busy={!!busy} profileSelected={!!profileId}
          onMonitor={(row,interval)=>action('Saving source monitoring',async()=>{
            await api('/company-sources/'+encodeURIComponent(row.id)+'/monitoring',{method:'PUT',body:JSON.stringify({source_version:row.source_version,revision:row.monitoring?.revision||0,poll_interval:interval})});
            setNotice('Monitoring saved. Automatic checks use enabled search profiles covering all sources.');
          })}
          onRun={row=>action('Refreshing employer source',async()=>{
            try {await queue('/company-sources/'+encodeURIComponent(row.id)+'/refresh',{profile_id:profileRef.current});}
            finally {await refresh(profileRef.current);}
          })}
          onSet={async(row,control,value)=>{
          await action('Updating company source',async()=>{
            const field=control==='pin'?'pinned':control==='watch'?'watched':'enabled';
            await api('/company-sources/'+encodeURIComponent(row.id)+'/'+control,{method:'PUT',body:JSON.stringify({[field]:value})});
            setNotice('Company source preference saved.');
          });
        }}/>
        {operationMode==='PERSONAL'&&<CompanyDiscoveryRequests rows={discoveryRequests} busy={!!busy}
          onReview={(row,website)=>action('Reviewing company website',async()=>{
            try {await queue('/company-discovery/requests/'+encodeURIComponent(row.id)+'/review',{website:website.trim(),revision:row.revision});}
            finally {await refresh(profileRef.current);}
          })}
          onDismiss={row=>action('Dismissing company request',async()=>{
            await api('/company-discovery/requests/'+encodeURIComponent(row.id)+'/dismiss',{method:'POST',body:JSON.stringify({revision:row.revision})});
            setNotice('Company discovery request dismissed.');
          })}/>}
        {operationMode==='PERSONAL'&&<CompanyDiscovery busy={!!busy} onDiscover={(companyName,companyWebsite)=>action('Discovering company jobs',()=>discoverCompany(companyName,companyWebsite))}/>}
        <section className="panel">
          <h2>Public Greenhouse board</h2>
          <p>Reads public job listings from the provider API. Each board keeps its own bounded schedule, timeout, rate limit, and backoff state. No login or application submission is performed.</p>
          <form onSubmit={e=>{e.preventDefault();void action('Saving source',async()=>{
            await api('/sources',{method:'PUT',body:JSON.stringify({tenant,enabled:true,poll_interval:sourceIntervalMinutes*60,concurrency:sourceConcurrency,request_timeout:sourceTimeout,rate_limit:sourceRate,jitter_seconds:sourceJitter})});
            setTenant('');setNotice('Source saved. Create a search profile below to enable scheduled discovery.');
          });}}>
            <div className="source-add">
              <label>Board token<input required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,79}" value={tenant} onChange={e=>setTenant(e.target.value)} placeholder="e.g. greenhouse"/></label>
              <label>Interval (minutes)<input type="number" min={5} max={10080} required value={sourceIntervalMinutes} onChange={e=>setSourceIntervalMinutes(Number(e.target.value))}/></label>
              <button disabled={!!busy}>Add board</button>
            </div>
            <div className="two-col">
              <label>Request timeout (seconds)<input type="number" min={2} max={60} value={sourceTimeout} onChange={e=>setSourceTimeout(Number(e.target.value))}/></label>
              <label>Rate limit (requests/minute)<input type="number" min={1} max={600} value={sourceRate} onChange={e=>setSourceRate(Number(e.target.value))}/></label>
              <label>Source concurrency<input type="number" min={1} max={8} value={sourceConcurrency} onChange={e=>setSourceConcurrency(Number(e.target.value))}/></label>
              <label>Schedule jitter (seconds)<input type="number" min={0} max={300} value={sourceJitter} onChange={e=>setSourceJitter(Number(e.target.value))}/></label>
            </div>
          </form>
        </section>
        {sources.map(source=><section className="panel" key={source.id}>
          <div className="toolbar"><div><h2>{source.name}</h2><p>{source.provider} / {source.tenant} · {source.jobs_seen} jobs collected</p></div><span className="tag">{source.enabled?source.health_status:'DISABLED'}</span></div>
          <p>{source.strategy} · every {source.poll_interval/60} minutes · timeout {source.request_timeout}s · rate {source.rate_limit}/min · concurrency {source.concurrency} · jitter ≤ {source.jitter_seconds}s</p>
          <p className="muted">Capabilities: {source.capabilities?.capabilities?.join(', ')||'Not declared'}</p>
          <p>Last success: {stamp(source.last_success)}</p>
          {source.backoff_until&&<p className="alert">Backoff until {stamp(source.backoff_until)}: {source.last_error}</p>}
          <button disabled={!!busy||!profileId||!source.enabled} onClick={()=>void action('Collecting public jobs',async()=>{await queue('/sources/'+source.id+'/collect',request());setTab('Jobs');})}>Collect now</button>{' '}
          <button className="quiet" disabled={!!busy} onClick={()=>void action('Updating source',async()=>{await api('/sources',{method:'PUT',body:JSON.stringify({tenant:source.tenant,enabled:!source.enabled,poll_interval:source.poll_interval,concurrency:source.concurrency,request_timeout:source.request_timeout,rate_limit:source.rate_limit,jitter_seconds:source.jitter_seconds})});})}>{source.enabled?'Disable':'Enable'}</button>
        </section>)}
        <section className="panel">
          <div className="toolbar"><div><h2>Scheduled search profiles</h2><p>Profiles are data-driven. The scheduler evaluates each enabled source independently and queues durable discovery work when it is due.</p></div><span className="tag">{searchProfiles.length} saved</span></div>
          {!profileId?<p className="muted">Select or create a career profile before adding a scheduled search.</p>:<form onSubmit={e=>{e.preventDefault();void action('Saving search profile',async()=>{
            const titles=searchTitles.split(/[\n,]+/).map(value=>value.trim()).filter(Boolean);
            await api('/search-profiles',{method:'PUT',body:JSON.stringify({id:crypto.randomUUID(),profile_id:profileId,name:searchName,enabled:true,include_titles:titles,remote:searchRemote,employment_types:searchEmployment?[searchEmployment]:[],source_ids:searchSource?[searchSource]:[]})});
            setSearchName('');setSearchTitles('');setNotice('Search profile saved. Due enabled sources will now be scheduled automatically.');
          });}}>
            <div className="two-col">
              <label>Search name<input required maxLength={200} value={searchName} onChange={e=>setSearchName(e.target.value)} placeholder="Remote Infrastructure"/></label>
              <label>Source<select value={searchSource} onChange={e=>setSearchSource(e.target.value)}><option value="">All enabled sources</option>{sources.map(source=><option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
              <label>Remote preference<select value={searchRemote} onChange={e=>setSearchRemote(e.target.value as 'ANY'|'REQUIRED'|'EXCLUDED')}><option value="ANY">Any</option><option value="REQUIRED">Remote required</option><option value="EXCLUDED">Exclude remote</option></select></label>
              <label>Employment type<select value={searchEmployment} onChange={e=>setSearchEmployment(e.target.value)}><option value="">Any</option><option value="FULL_TIME">Full-time</option><option value="PART_TIME">Part-time</option><option value="CONTRACT">Contract</option><option value="CONTRACT_TO_HIRE">Contract-to-hire</option><option value="TEMPORARY">Temporary</option><option value="INTERNSHIP">Internship</option><option value="OTHER">Other</option></select></label>
            </div>
            <label>Include titles<textarea required value={searchTitles} onChange={e=>setSearchTitles(e.target.value)} placeholder={"System Administrator\nInfrastructure Engineer\nNOC Engineer"}/></label>
            <button disabled={!!busy||sources.length===0}>Save scheduled search</button>
          </form>}
          {searchProfiles.map(search=><div className="document" key={search.id}>
            <div className="toolbar"><div><strong>{search.name}</strong><p>{search.criteria.include_titles.join(', ')||'Any title'} · remote {search.criteria.remote.toLowerCase()} · {search.criteria.employment_types.join(', ')||'any employment type'}</p></div><span className="tag">{search.enabled?'ENABLED':'PAUSED'}</span></div>
            <p className="muted">{search.source_ids.length?(search.source_ids.length+' selected source(s)'):'All enabled sources'} · updated {stamp(search.updated_at)}</p>
            <button type="button" className="quiet" disabled={!!busy} onClick={()=>void action(search.enabled?'Pausing search':'Resuming search',async()=>{await api('/search-profiles',{method:'PUT',body:JSON.stringify({id:search.id,profile_id:search.profile_id,name:search.name,enabled:!search.enabled,include_titles:search.criteria.include_titles,remote:search.criteria.remote,employment_types:search.criteria.employment_types,source_ids:search.source_ids})});})}>{search.enabled?'Pause':'Resume'}</button>
          </div>)}
        </section>
        {operationMode==='FIXTURE'&&<section className="panel"><h2>Fictional local source</h2><p>Use the demo to test document preparation, dry-run, mock confirmation and tracking without contacting an employer.</p><button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>}
      </>}
      {tab==='Applications'&&<div className="job-grid"><section><h2>{apps.length} prepared applications</h2>{apps.map(application=><button className="job-card" key={application.id} onClick={()=>void showApp(application.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{application.company}</span><span className="tag">{application.target==='MOCK'?'Fictional mock':'Manual only'}</span></div><h2>{application.title}</h2><p>{application.state} · documents v{application.document_version}</p></button>)}{!apps.length&&<p className="empty">Select a job and prepare documents to start tracking it here.</p>}</section><section className="job-detail">{selectedApp?<><h2>Application details</h2><span className="tag">{selectedApp.state}</span><p>Document revision {selectedApp.document_version} · {selectedApp.fact_ids.length} approved facts</p><h3>Documents</h3><DocumentHistory documents={docs}/>
        {selectedApp.target==='MOCK'?(operationMode==='FIXTURE'?<section className="mock-box"><h3>Controlled fictional test</h3><p>The dry-run fills and reviews the local mock form, then stops before submission.</p><button disabled={!!busy||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Running mock dry-run',async()=>{await queue('/applications/'+selectedApp.id+'/mock-run',{submit:false});await showApp(selectedApp.id);})}>Run dry-run</button><label className="consent"><input type="checkbox" checked={mockConsent} onChange={e=>setMockConsent(e.target.checked)}/> I approve one fictional mock submission</label><button disabled={!!busy||!mockConsent||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Submitting to local mock',async()=>{await queue('/applications/'+selectedApp.id+'/mock-run',{submit:true});await showApp(selectedApp.id);})}>Submit to local mock</button></section>:<p className="safety">Fictional application actions are disabled in personal mode.</p>):<p className="safety">Manual preparation only. Review the documents and use the employer’s own application page.</p>}
        <h3>Tracking evidence</h3><p className="description">{selectedApp.trace.join(' → ')}</p>{selectedApp.confirmation&&<pre>{JSON.stringify(selectedApp.confirmation,null,2)}</pre>}<h3>Attempts</h3>{selectedApp.attempts?.map((attempt,i)=><p key={i}>{stamp(attempt.started_at)} · {attempt.state} · {attempt.steps.join(' → ')}</p>)}</>:<div className="empty"><h2>Your paper trail.</h2><p>Select an application to download documents and inspect its status and attempts.</p></div>}</section></div>}
      {tab==='Activity'&&<><section className="panel"><div className="toolbar"><h2>Service health</h2><button onClick={()=>{void readiness();void refresh().catch(e=>setError(String(e)));}}>Refresh activity</button></div>{ready&&Object.entries(ready.checks).map(([name,ok])=><p key={name}><span className={'status '+(ok?'good':'warn')}>{ok?'Ready':'Unavailable'}</span> {name}</p>)}<p className="muted">Queued work is durable. A browser interruption before submission requires review; uncertain submission outcomes require reconciliation.</p></section><section className="panel"><h2>Background operations</h2>{ops.map(op=><div className="operation" key={op.id}><div><strong>{op.kind}</strong><p>{op.state} · {op.attempt_count} attempt(s) · {stamp(op.updated_at)}</p>{op.error&&<p className="error-text">{op.error}</p>}<code>{op.id.slice(0,16)}</code></div>{!terminal.has(op.state)&&<button onClick={()=>void api('/operations/'+op.id+'/cancel',{method:'POST'}).then(()=>refresh()).catch(e=>setError(String(e)))}>{op.cancel_requested?'Cancellation requested':'Cancel'}</button>}</div>)}{!ops.length&&<p>No operations for this profile yet.</p>}</section></>}
      <footer>MJobs · Your data stays in this local workspace. User-testing milestone; broader integrations and automation are still in development.</footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
