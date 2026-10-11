import React, { useEffect, useRef, useState } from 'react';
import { api, uploadResume } from './api';
import type { ImportedFact, ImportedContact } from './profile-import';

export type Resume = {id:string;profile_id:string;filename:string;format:string;size_bytes:number;sha256:string;status:string;created_at:string;series_id:string;label:string;version:number};
export type Extraction = {status:string;warnings:string[];contact:ImportedContact;facts:ImportedFact[];
  date_ranges?:{fact_id:string;raw:string;start:string;end:string}[]};
type DurableExtraction = {operation_id:string;request_id?:string;state:string;current_step:string|null;progress:number;
  error:string|null;updated_at?:string;completed_at:string|null;result?:Extraction};

const extractionPath = (profileId:string,resumeId:string,operationId?:string) =>
  '/profiles/'+encodeURIComponent(profileId)+'/resumes/'+encodeURIComponent(resumeId)+
  '/extract/queued'+(operationId?'/'+encodeURIComponent(operationId):'');
const extractionIntentKey = (profileId:string,resumeId:string) =>
  'mjobs.resume-extraction-intent.v1:'+encodeURIComponent(profileId)+':'+encodeURIComponent(resumeId);

export function ResumeLibrary({profileId, saved, resumes, busy, action, onStored, onApply}: {
  profileId:string; saved:boolean; resumes:Resume[]; busy:boolean;
  action:(label:string, run:()=>Promise<void>)=>Promise<void>;
  onStored:(resumes:Resume[])=>void;
  onApply:(extraction:Extraction, facts:Set<string>, contact:Set<string>)=>void;
}) {
  const [file,setFile] = useState<File|null>(null);
  const [label,setLabel] = useState('');
  const [series,setSeries] = useState('');
  const [extraction,setExtraction] = useState<Extraction|null>(null);
  const [extractingResume,setExtractingResume] = useState('');
  const [extractionProgress,setExtractionProgress] = useState<DurableExtraction|null>(null);
  const [cancelNotice,setCancelNotice] = useState('');
  const [selectedFacts,setSelectedFacts] = useState<Set<string>>(new Set());
  const [selectedContact,setSelectedContact] = useState<Set<string>>(new Set());
  const input = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const generation = useRef(0);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    const current=++generation.current;
    setExtraction(null);setExtractionProgress(null);setExtractingResume('');
    setSelectedFacts(new Set());setSelectedContact(new Set());setCancelNotice('');
    let active=true;
    async function restoreLatest() {
      const candidates=await Promise.all(resumes.map(async resume=>{
        const key=extractionIntentKey(profileId,resume.id);
        const intent=window.localStorage.getItem(key);
        let status:DurableExtraction|null=null;
        try { status=await api<DurableExtraction>(extractionPath(profileId,resume.id)); } catch {}
        if(intent&&status?.request_id!==intent) {
          try { status=await api<DurableExtraction>(extractionPath(profileId,resume.id),{
            method:'POST',body:JSON.stringify({request_id:intent}),
          }); } catch {}
        }
        return status?{resume,status}:null;
      }));
      if(!active||generation.current!==current)return;
      const latest=candidates.filter((item):item is {resume:Resume;status:DurableExtraction}=>item!==null)
        .sort((a,b)=>(b.status.updated_at||'').localeCompare(a.status.updated_at||''))[0];
      if(!latest)return;
      if(!['QUEUED','RUNNING'].includes(latest.status.state)
          &&window.localStorage.getItem(extractionIntentKey(profileId,latest.resume.id))===latest.status.request_id)
        window.localStorage.removeItem(extractionIntentKey(profileId,latest.resume.id));
      if(latest.status.state==='COMPLETED'&&latest.status.result){
        setExtraction(latest.status.result);setExtractionProgress(latest.status);
      } else if(latest.status.state==='QUEUED'||latest.status.state==='RUNNING') {
        setExtractingResume(latest.resume.id);void pollExtraction(latest.resume,latest.status.operation_id,current)
          .finally(()=>{if(mounted.current&&generation.current===current)setExtractingResume('');});
      }
    }
    void restoreLatest();
    return ()=>{active=false;if(generation.current===current)generation.current++;};
  },[profileId,resumes]);
  const groups = Array.from(new Map(resumes.map(resume=>[resume.series_id,resume.label])).entries());
  const toggle = (setter:React.Dispatch<React.SetStateAction<Set<string>>>, key:string, checked:boolean) =>
    setter(current=>{const next=new Set(current);if(checked)next.add(key);else next.delete(key);return next;});

  async function store() {
    if (!file) return;
    await uploadResume(profileId,file,{label:series?'':label,seriesId:series});
    const result = await api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(profileId)+'/resumes');
    if (!mounted.current) return;
    onStored(result.resumes);setFile(null);setLabel('');setExtraction(null);setExtractionProgress(null);
    if(input.current)input.current.value='';
  }
  async function extract(resume:Resume) {
    const current=++generation.current;
    setExtractingResume(resume.id);setExtraction(null);setExtractionProgress(null);
    setSelectedFacts(new Set());setSelectedContact(new Set());setCancelNotice('');
    try {
      const key=extractionIntentKey(profileId,resume.id);
      const request_id=window.localStorage.getItem(key)||crypto.randomUUID();
      window.localStorage.setItem(key,request_id);
      const queued=await api<DurableExtraction>(extractionPath(profileId,resume.id),{
        method:'POST',body:JSON.stringify({request_id}),
      });
      if (!mounted.current||generation.current!==current) return;
      setExtractionProgress(queued);
      await pollExtraction(resume,queued.operation_id,current);
    } finally {
      if (mounted.current&&generation.current===current) setExtractingResume('');
    }
  }
  async function pollExtraction(resume:Resume,operationId:string,current:number) {
    while(mounted.current&&generation.current===current) {
      const status=await api<DurableExtraction>(extractionPath(profileId,resume.id,operationId));
      if(!mounted.current||generation.current!==current)return;
      setExtractionProgress(status);
      if(status.state==='COMPLETED'){
        if(status.result){setExtraction(status.result);setSelectedFacts(new Set());setSelectedContact(new Set());}
        if(window.localStorage.getItem(extractionIntentKey(profileId,resume.id))===status.request_id)
          window.localStorage.removeItem(extractionIntentKey(profileId,resume.id));
        return;
      }
      if(['FAILED','BLOCKED','CANCELLED','DEAD_LETTERED','DISCARDED'].includes(status.state)){
        if(window.localStorage.getItem(extractionIntentKey(profileId,resume.id))===status.request_id)
          window.localStorage.removeItem(extractionIntentKey(profileId,resume.id));
        setCancelNotice(status.state==='CANCELLED'?'Resume extraction was cancelled.':
          'Resume extraction ended with '+status.state.replaceAll('_',' ').toLowerCase()+'.');
        return;
      }
      await new Promise(resolve=>window.setTimeout(resolve,600));
    }
  }
  async function cancelExtraction(resume:Resume) {
    try {
      const result = await api<{cancelled:boolean}>('/profiles/'+encodeURIComponent(profileId)+'/resumes/'+encodeURIComponent(resume.id)+'/extract/cancel',{method:'POST'});
      if (mounted.current) setCancelNotice(result.cancelled?'Resume extraction cancellation requested.':'Resume extraction has already finished.');
    } catch (error) {
      if (mounted.current) setCancelNotice(error instanceof Error?error.message:'Could not cancel resume extraction.');
    }
  }
  return <section className="panel"><h2>Resume history</h2>
    <p>Keep several named resumes and their original versions. Extract a saved file to review suggestions for your profile.</p>
    {!saved?<p>Save your name and email first, then upload a resume. You can leave career facts empty.</p>:<>
      <label>Choose a resume (up to 10 MiB)<input ref={input} type="file" accept=".pdf,.docx,.txt" disabled={busy} onChange={e=>setFile(e.target.files?.[0]||null)}/></label>
      <div className="two-col">
        <label>Resume collection<select aria-label="Resume collection" value={series} disabled={busy} onChange={e=>setSeries(e.target.value)}>
          <option value="">New resume</option>{groups.map(([id,name])=><option key={id} value={id}>{name}</option>)}
        </select></label>
        {!series&&<label>Resume label<input maxLength={200} placeholder="e.g. Engineering" value={label} onChange={e=>setLabel(e.target.value)}/></label>}
      </div>
      <button type="button" disabled={busy||!file} onClick={()=>void action('Storing base resume',store)}>Store resume</button>
      {groups.map(([id,name])=><section key={id} data-resume-series={id} className="resume-group"><h3>{name}</h3>
        {resumes.filter(r=>r.series_id===id).sort((a,b)=>b.version-a.version).map(resume=><div className="document" key={id+resume.id} data-resume-version={resume.version}>
          <strong>Version {resume.version} · {resume.filename}</strong>
          <p>{resume.status.replaceAll('_',' ')} · {resume.format.toUpperCase()} · stored {new Date(resume.created_at).toLocaleString()}</p>
          {extractingResume===resume.id?<><button type="button" onClick={()=>void cancelExtraction(resume)}>Cancel extraction</button>
            <p role="status">Extraction {extractionProgress?.state?.toLowerCase()||'queued'} · {extractionProgress?.progress||0}%</p></>:
            <button type="button" disabled={busy} onClick={()=>void action('Queueing resume extraction',()=>extract(resume))}>Extract and review</button>}{' '}
          <a className="button-link" href={'/profiles/'+encodeURIComponent(profileId)+'/resumes/'+encodeURIComponent(resume.id)}>Download original</a>
        </div>)}
      </section>)}
    </>}
    {cancelNotice&&<p role="status">{cancelNotice}</p>}
    {extraction&&<div className="extraction-review" role="region" aria-label="Resume extraction review">
      <h3>Extraction review: {extraction.status.replaceAll('_',' ')}</h3>
      <p>Pending suggestions from the saved resume. Select contact fields to replace and career facts to add. Nothing changes in your profile until you choose Add selected to profile editor; imported facts still require verification and approval.</p>
      {extraction.warnings.map((warning,i)=><p className="alert" key={i}>{warning}</p>)}
      {Object.entries(extraction.contact).filter(([,v])=>Array.isArray(v)?v.length>0:typeof v==='string'&&v.trim()).map(([key,value])=><label className="fact-suggestion" key={key}>
        <input type="checkbox" checked={selectedContact.has(key)} onChange={e=>toggle(setSelectedContact,key,e.target.checked)}/>
        Add contact {key.replaceAll('_',' ')}: {Array.isArray(value)?value.join(', '):String(value)}
      </label>)}
      {extraction.facts.map(fact=><label className="fact-suggestion" key={fact.id}>
        <input type="checkbox" checked={selectedFacts.has(fact.id!)} onChange={e=>toggle(setSelectedFacts,fact.id!,e.target.checked)}/>
        <span><strong>{fact.kind}</strong> {fact.text}<small>Unverified suggestion · confidence {fact.confidence}%</small>
          {extraction.date_ranges?.filter(period=>period.fact_id===fact.id).map(period=><small key={period.fact_id}>
            Dates to review: {period.start} to {period.end==='PRESENT'?'Present':period.end}. Original: {period.raw}
          </small>)}
        </span>
      </label>)}
      {extraction.status==='READY_FOR_REVIEW'&&<button type="button" className="primary" disabled={busy} onClick={()=>{onApply(extraction,selectedFacts,selectedContact);setExtraction(null);}}>Add selected to profile editor</button>}
      <button type="button" className="quiet" onClick={()=>setExtraction(null)}>Dismiss</button>
    </div>}
  </section>;
}
