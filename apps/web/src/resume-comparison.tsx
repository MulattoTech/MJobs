import React,{useState} from 'react';
import {api} from './api';
import type {GeneratedDocument} from './document-history';

type Fact={fact_id:string;kind:string;text:string};
type Change={fact_id:string;before:Fact|null;after:Fact|null};
export type Comparison={
  base:{id:string;version:number;text:string};tailored:{id:string;version:number;text:string};
  summary_changes:Change[];bullet_changes:Change[];
  reordered:{fact_id:string;text:string;before_position:number;after_position:number}[];
  emphasized_skills:(Fact&{job_skills:string[]})[];omitted:Fact[];added:Fact[];
  contact_changed:boolean;scope:string;
};

function Changes({title,rows}:{title:string;rows:Change[]}) {
  return <section aria-label={title}><h4>{title}</h4>{rows.length?<ul>{rows.map(row=><li key={row.fact_id}>
    Before: {row.before?.text||'Not present'}<br/>After: {row.after?.text||'Not present'}
  </li>)}</ul>:<p>No changes.</p>}</section>;
}

export function ComparisonResult({result}:{result:Comparison}) {
  return <section aria-label="Resume comparison result">
    <p>{result.scope}</p>
    <Changes title="Summary changes" rows={result.summary_changes}/>
    <Changes title="Bullet and fact changes" rows={result.bullet_changes}/>
    <section aria-label="Reordering"><h4>Reordering</h4>{result.reordered.length?<ul>{result.reordered.map(row=><li key={row.fact_id}>{row.text}: fact position {row.before_position} → {row.after_position}</li>)}</ul>:<p>No shared facts reordered.</p>}</section>
    <section aria-label="Skills emphasized"><h4>Skills emphasized</h4><p>Target matches used for ordering; no wording or qualifications inferred.</p>{result.emphasized_skills.length?<ul>{result.emphasized_skills.map(row=><li key={row.fact_id}>{row.text} — matched {row.job_skills.join(', ')}</li>)}</ul>:<p>No matched skills recorded.</p>}</section>
    <section aria-label="Content omitted"><h4>Content omitted</h4>{result.omitted.length?<ul>{result.omitted.map(row=><li key={row.fact_id}>{row.kind}: {row.text}</li>)}</ul>:<p>No content omitted.</p>}</section>
    <section aria-label="Content added"><h4>Content added</h4>{result.added.length?<ul>{result.added.map(row=><li key={row.fact_id}>{row.kind}: {row.text}</li>)}</ul>:<p>No content added.</p>}</section>
    <p>{result.contact_changed?'Contact details changed; review both recorded versions below.':'Contact details unchanged.'}</p>
    <div className="resume-comparison-texts">
      <section><h4>Generated base · v{result.base.version}</h4><pre>{result.base.text}</pre></section>
      <section><h4>Tailored resume · v{result.tailored.version}</h4><pre>{result.tailored.text}</pre></section>
    </div>
  </section>;
}

export function ResumeComparison({documents}:{documents:GeneratedDocument[]}) {
  const bases=documents.filter(row=>row.kind==='resume'&&row.template==='verified-facts-v1'&&row.generation_model==='deterministic-fact-copy-v1');
  const targets=documents.filter(row=>row.kind==='resume'&&row.template==='approved-skill-order-v1'&&row.generation_model==='deterministic-skill-order-v1'&&row.generation_context?.mode==='APPROVED_SKILL_ORDER');
  const [base,setBase]=useState(bases[0]?.id||'');const [target,setTarget]=useState(targets[0]?.id||'');
  const [result,setResult]=useState<Comparison|null>(null);const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  if(!bases.length||!targets.length)return <p>Base ↔ tailored comparison needs both generated resume versions for this application. Uploaded originals are not compared.</p>;
  async function compare(){
    setBusy(true);setError('');setResult(null);
    try{
      const row=bases.find(row=>row.id===base);
      if(!row?.application_id)throw new Error('Original application history is unavailable.');
      setResult(await api<Comparison>('/applications/'+encodeURIComponent(row.application_id)+'/document-comparison?'+new URLSearchParams({base_document_id:base,tailored_document_id:target})));
    }catch(e){setError(String(e));}finally{setBusy(false);}
  }
  return <section aria-label="Compare base and tailored resumes">
    <h3>Base ↔ tailored resume</h3>
    <label>Generated base version<select aria-label="Generated base version" value={base} disabled={busy} onChange={e=>{setBase(e.target.value);setResult(null);setError('');}}>{bases.map(row=><option key={row.id} value={row.id}>v{row.version}</option>)}</select></label>
    <label>Tailored version<select aria-label="Tailored version" value={target} disabled={busy} onChange={e=>{setTarget(e.target.value);setResult(null);setError('');}}>{targets.map(row=><option key={row.id} value={row.id}>v{row.version}</option>)}</select></label>
    <button disabled={busy} onClick={()=>void compare()}>{busy?'Comparing…':'Compare selected resumes'}</button>
    {error&&<p role="alert">Comparison unavailable: {error}</p>}
    {result&&<ComparisonResult result={result}/>}
  </section>;
}
