import React,{useEffect,useState} from 'react';
import {api} from './api';
import type {GeneratedDocument} from './document-history';

type Fact={fact_id:string;kind:string;text:string};
type Change={fact_id:string;before:Fact|null;after:Fact|null};
export type Comparison={
  base:{id:string;version:number;text:string;source_kind?:string;filename?:string;format?:string;sha256?:string;profile_id?:string;extraction?:{status:string;warnings:string[];used_ocr:boolean}};
  tailored:{id:string;version:number;text:string};
  summary_changes:Change[];bullet_changes:Change[];
  reordered:{fact_id:string;text:string;before_position:number;after_position:number}[];
  emphasized_skills:(Fact&{job_skills:string[]})[];omitted:Fact[];added:Fact[];
  contact_changed:boolean|null;scope:string;
  section_availability?:{summary_changes:boolean;bullet_changes:boolean};
};
type UploadedBase={revision_id:string;version:number;label:string;filename:string};

function Changes({title,rows,available=true}:{title:string;rows:Change[];available?:boolean}) {
  return <section aria-label={title}><h4>{title}</h4>{!available?<p>Source section unavailable or ambiguous; review the original text.</p>:rows.length?<ul>{rows.map(row=><li key={row.fact_id}>
    Before: {row.before?.text||'Not present'}<br/>After: {row.after?.text||'Not present'}
  </li>)}</ul>:<p>No changes.</p>}</section>;
}

export function ComparisonResult({result}:{result:Comparison}) {
  const uploaded=result.base.source_kind==='UPLOADED';
  return <section aria-label="Resume comparison result">
    <p>{result.scope}</p>
    {uploaded&&<section aria-label="Original extraction provenance"><p>{result.base.filename} · {result.base.format?.toUpperCase()} · uploaded version {result.base.version}</p>
      <p>{result.base.extraction?.used_ocr?'OCR text is uncertain; verify against the original file.':'Native extracted text; formatting may affect comparison.'}</p>
      {result.base.extraction?.warnings.map((warning,index)=><p role="note" key={index}>{warning}</p>)}
      <a className="button-link" href={'/profiles/'+encodeURIComponent(result.base.profile_id||'')+'/document-library/'+encodeURIComponent(result.base.id)}>Download compared original</a>
      <details><summary>Original file checksum</summary><code style={{overflowWrap:'anywhere'}}>{result.base.sha256}</code></details>
    </section>}
    <Changes title="Summary changes" rows={result.summary_changes} available={result.section_availability?.summary_changes}/>
    <Changes title="Bullet and fact changes" rows={result.bullet_changes} available={result.section_availability?.bullet_changes}/>
    <section aria-label="Reordering"><h4>Reordering</h4>{result.reordered.length?<ul>{result.reordered.map(row=><li key={row.fact_id}>{row.text}: {uploaded?'text item':'fact'} position {row.before_position} → {row.after_position}</li>)}</ul>:<p>No matched content reordered.</p>}</section>
    <section aria-label="Skills emphasized"><h4>Skills emphasized</h4><p>Target matches used for ordering; no wording or qualifications inferred.</p>{result.emphasized_skills.length?<ul>{result.emphasized_skills.map(row=><li key={row.fact_id}>{row.text} — matched {row.job_skills.join(', ')}</li>)}</ul>:<p>No matched skills recorded.</p>}</section>
    <section aria-label="Content omitted"><h4>Content omitted</h4>{uploaded&&<p>Extracted items with no exact match in the output; changes in wording or layout may explain them.</p>}{result.omitted.length?<ul>{result.omitted.map(row=><li key={row.fact_id}>{row.kind}: {row.text}</li>)}</ul>:<p>{uploaded?'No unmatched extracted items.':'No content omitted.'}</p>}</section>
    <section aria-label="Content added"><h4>Content added</h4>{result.added.length?<ul>{result.added.map(row=><li key={row.fact_id}>{row.kind}: {row.text}</li>)}</ul>:<p>No content added.</p>}</section>
    <p>{result.contact_changed===null?'Review contact details in both original and generated text below.':result.contact_changed?'Contact details changed; review both recorded versions below.':'Contact details unchanged.'}</p>
    <div className="resume-comparison-texts">
      <section><h4>{uploaded?'Uploaded':'Generated'} base · v{result.base.version}</h4><pre>{result.base.text}</pre></section>
      <section><h4>Tailored resume · v{result.tailored.version}</h4><pre>{result.tailored.text}</pre></section>
    </div>
  </section>;
}

export function ResumeComparison({documents}:{documents:GeneratedDocument[]}) {
  const bases=documents.filter(row=>row.kind==='resume'&&row.template==='verified-facts-v1'&&row.generation_model==='deterministic-fact-copy-v1');
  const targets=documents.filter(row=>row.kind==='resume'&&row.template==='approved-skill-order-v1'&&row.generation_model==='deterministic-skill-order-v1'&&row.generation_context?.mode==='APPROVED_SKILL_ORDER');
  const [base,setBase]=useState(bases[0]?'g:'+bases[0].id:'');const [target,setTarget]=useState(targets[0]?.id||'');
  const [result,setResult]=useState<Comparison|null>(null);const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const [uploaded,setUploaded]=useState<UploadedBase[]>([]);const [loading,setLoading]=useState(false);const [loadError,setLoadError]=useState('');
  const [refresh,setRefresh]=useState(0);const profileId=documents[0]?.profile_id;
  useEffect(()=>{
    if(!profileId)return;
    const controller=new AbortController();let active=true;setLoading(true);setLoadError('');
    void api<{resumes:UploadedBase[]}>('/profiles/'+encodeURIComponent(profileId)+'/resumes',{signal:controller.signal})
      .then(data=>{if(active){setUploaded(data.resumes);setBase(value=>value||(data.resumes[0]?'u:'+data.resumes[0].revision_id:''));}})
      .catch(e=>{if(active)setLoadError(String(e));}).finally(()=>{if(active)setLoading(false);});
    return ()=>{active=false;controller.abort();};
  },[profileId,refresh]);
  if(!targets.length)return <p>Base ↔ tailored comparison needs a generated tailored resume for this application.</p>;
  async function compare(){
    setBusy(true);setError('');setResult(null);
    try{
      const row=targets.find(row=>row.id===target);
      if(!row?.application_id)throw new Error('Original application history is unavailable.');
      const fromUpload=base.startsWith('u:');
      const params:Record<string,string>=fromUpload?{base_revision_id:base.slice(2),tailored_document_id:target}:{base_document_id:base.slice(2),tailored_document_id:target};
      setResult(await api<Comparison>('/applications/'+encodeURIComponent(row.application_id)+(fromUpload?'/uploaded-resume-comparison?':'/document-comparison?')+new URLSearchParams(params)));
    }catch(e){setError(String(e));}finally{setBusy(false);}
  }
  return <section aria-label="Compare base and tailored resumes">
    <h3>Base ↔ tailored resume</h3>
    <label>Base resume<select aria-label="Base resume" value={base} disabled={busy} onChange={e=>{setBase(e.target.value);setResult(null);setError('');}}>
      {!base&&<option value="">Choose a base resume</option>}
      <optgroup label="Uploaded originals">{uploaded.map(row=><option key={row.revision_id} value={'u:'+row.revision_id}>{row.label} · v{row.version} · {row.filename}</option>)}</optgroup>
      <optgroup label="Generated baselines">{bases.map(row=><option key={row.id} value={'g:'+row.id}>v{row.version}</option>)}</optgroup>
    </select></label>
    <button disabled={busy||loading} onClick={()=>setRefresh(value=>value+1)}>Refresh base resumes</button>
    {loading&&<p role="status">Loading uploaded base resumes…</p>}{loadError&&<p role="alert">Uploaded baselines unavailable: {loadError}</p>}
    {!loading&&!bases.length&&!uploaded.length&&<p>Upload a base resume in Profile, then refresh this list.</p>}
    <label>Tailored version<select aria-label="Tailored version" value={target} disabled={busy} onChange={e=>{setTarget(e.target.value);setResult(null);setError('');}}>{targets.map(row=><option key={row.id} value={row.id}>v{row.version}</option>)}</select></label>
    <button disabled={busy||!base} onClick={()=>void compare()}>{busy?'Comparing…':'Compare selected resumes'}</button>
    {error&&<p role="alert">Comparison unavailable: {error}</p>}
    {result&&<ComparisonResult result={result}/>}
  </section>;
}
