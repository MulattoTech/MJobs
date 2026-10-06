import React, { useEffect, useRef, useState } from 'react';
import { api, uploadResume } from './api';
import type { ImportedFact, ImportedContact } from './profile-import';

export type Resume = {id:string;profile_id:string;filename:string;format:string;size_bytes:number;sha256:string;status:string;created_at:string;series_id:string;label:string;version:number};
export type Extraction = {status:string;warnings:string[];contact:ImportedContact;facts:ImportedFact[];
  date_ranges?:{fact_id:string;raw:string;start:string;end:string}[]};

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
  const [cancelNotice,setCancelNotice] = useState('');
  const [selectedFacts,setSelectedFacts] = useState<Set<string>>(new Set());
  const [selectedContact,setSelectedContact] = useState<Set<string>>(new Set());
  const input = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  const groups = Array.from(new Map(resumes.map(resume=>[resume.series_id,resume.label])).entries());
  const toggle = (setter:React.Dispatch<React.SetStateAction<Set<string>>>, key:string, checked:boolean) =>
    setter(current=>{const next=new Set(current);if(checked)next.add(key);else next.delete(key);return next;});

  async function store() {
    if (!file) return;
    await uploadResume(profileId,file,{label:series?'':label,seriesId:series});
    const result = await api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(profileId)+'/resumes');
    if (!mounted.current) return;
    onStored(result.resumes);setFile(null);setLabel('');setExtraction(null);
    if(input.current)input.current.value='';
  }
  async function extract(resume:Resume) {
    setExtractingResume(resume.id);setCancelNotice('');
    try {
      const result = await api<Extraction>('/profiles/'+encodeURIComponent(profileId)+'/resumes/'+encodeURIComponent(resume.id)+'/extract',{method:'POST'});
      if (!mounted.current) return;
      setExtraction(result);setSelectedFacts(new Set(result.facts.map(f=>f.id!)));setSelectedContact(new Set());
    } finally {
      if (mounted.current) setExtractingResume('');
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
          {extractingResume===resume.id?<button type="button" onClick={()=>void cancelExtraction(resume)}>Cancel extraction</button>:<button type="button" disabled={busy} onClick={()=>void action('Extracting resume',()=>extract(resume))}>Extract and review</button>}{' '}
          <a className="button-link" href={'/profiles/'+encodeURIComponent(profileId)+'/resumes/'+encodeURIComponent(resume.id)}>Download original</a>
        </div>)}
      </section>)}
    </>}
    {cancelNotice&&<p role="status">{cancelNotice}</p>}
    {extraction&&<div className="extraction-review" role="region" aria-label="Resume extraction review">
      <h3>Extraction review: {extraction.status.replaceAll('_',' ')}</h3>
      <p>Select contact fields to replace, and career facts to add. You can edit them in the profile before saving. Imported facts require your verification and approval.</p>
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
