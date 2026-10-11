import React,{useEffect,useRef,useState} from 'react';
import {api,terminal} from './api';
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
type ComparisonOperation={operation_id:string;state:string;current_step:string|null;progress:number|null;error:string|null;result?:Comparison};

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
    <section aria-label="Skills and bullets emphasized"><h4>Skills and bullets emphasized</h4><p>Exact job-skill matches in approved facts are used for ordering or visual emphasis; no wording or qualifications are inferred.</p>{result.emphasized_skills.length?<ul>{result.emphasized_skills.map(row=><li key={row.fact_id}>{row.text} — matched {row.job_skills.join(', ')}</li>)}</ul>:<p>No matched skills or bullets recorded.</p>}</section>
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
  const bases=documents.filter(row=>row.kind==='resume'&&(
    (row.template==='verified-facts-v1'&&(row.generation_model==='deterministic-fact-copy-v1'||row.generation_model===null))||
    (row.template==='verified-headings-v1'&&row.generation_model==='deterministic-fact-copy-v2')||
    (row.template==='approved-contact-base-v1'&&row.generation_model==='deterministic-contact-copy-v1')));
  const targets=documents.filter(row=>row.kind==='resume'&&row.generation_context?.mode==='APPROVED_SKILL_ORDER'&&(
    (row.template==='approved-skill-order-v1'&&row.generation_model==='deterministic-skill-order-v1')||
    (row.template==='approved-skill-order-headings-v1'&&row.generation_model==='deterministic-skill-order-v2')||
    (row.template==='approved-fact-selection-headings-v1'&&row.generation_model==='deterministic-fact-selection-v1')||
    (row.template==='approved-skill-emphasis-v1'&&row.generation_model==='deterministic-skill-order-v3')||
    (row.template==='approved-selection-emphasis-v1'&&row.generation_model==='deterministic-fact-selection-v2')||
    (row.template==='approved-skill-and-bullet-emphasis-v2'&&row.generation_model==='deterministic-skill-and-bullet-emphasis-v1')||
    (row.template==='approved-selection-and-bullet-emphasis-v2'&&row.generation_model==='deterministic-fact-selection-and-bullet-emphasis-v1')||
    (row.template==='approved-contact-emphasis-v1'&&row.generation_model==='deterministic-contact-order-v1')||
    (row.template==='approved-contact-selection-v1'&&row.generation_model==='deterministic-contact-selection-v1')||
    (row.template==='approved-contact-skill-and-bullet-emphasis-v2'&&row.generation_model==='deterministic-contact-skill-and-bullet-emphasis-v1')||
    (row.template==='approved-summary-skill-and-bullet-emphasis-v3'&&row.generation_model==='deterministic-summary-skill-and-bullet-emphasis-v2')||
    (row.template==='approved-contact-summary-skill-and-bullet-emphasis-v3'&&row.generation_model==='deterministic-contact-summary-skill-and-bullet-emphasis-v2')||
    (row.template==='approved-target-relevant-bullets-summary-skill-and-bullet-emphasis-v4'&&row.generation_model==='deterministic-target-relevant-bullets-summary-skill-order-v3')||
    (row.template==='approved-contact-target-relevant-bullets-summary-skill-and-bullet-emphasis-v4'&&row.generation_model==='deterministic-contact-target-relevant-bullets-summary-skill-order-v3')||
    (row.template==='approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5'&&row.generation_model==='deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4')||
    (row.template==='approved-contact-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5'&&row.generation_model==='deterministic-contact-target-relevant-bullets-summary-skill-responsibility-order-v4')||
    (row.template==='approved-contact-selection-and-bullet-emphasis-v2'&&row.generation_model==='deterministic-contact-selection-and-bullet-emphasis-v1')||
    (row.template==='approved-selection-user-rephrase-v1'&&row.generation_model==='user-reviewed-factual-rephrase-v1')||
    (row.template==='approved-contact-selection-user-rephrase-v1'&&row.generation_model==='user-reviewed-factual-rephrase-v1')||
    (row.template==='approved-selection-user-reviewed-adjustment-v2'&&row.generation_model==='user-reviewed-factual-adjustment-v2')||
    (row.template==='approved-contact-selection-user-reviewed-adjustment-v2'&&row.generation_model==='user-reviewed-factual-adjustment-v2')));
  const [base,setBase]=useState(bases[0]?'g:'+bases[0].id:'');const [target,setTarget]=useState(targets[0]?.id||'');
  const [result,setResult]=useState<Comparison|null>(null);const [error,setError]=useState('');const [notice,setNotice]=useState('');const [busy,setBusy]=useState(false);
  const comparisonController=useRef<AbortController|null>(null);
  const operationRef=useRef<{url:string;id:string}|null>(null);
  const [operationReady,setOperationReady]=useState(false);
  const requestRef=useRef<{key:string;id:string}|null>(null);
  const [recovery,setRecovery]=useState(0);
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
  function operationUrl(row:GeneratedDocument){
    return '/profiles/'+encodeURIComponent(profileId||'')+'/applications/'+encodeURIComponent(row.application_id||'')+'/uploaded-resume-comparison/operations';
  }
  async function observe(url:string,params:URLSearchParams,controller:AbortController,initial?:ComparisonOperation){
    // Admission responses carry status, while the scoped GET owns result readback.
    let operation=initial?.state==='COMPLETED'&&!initial.result?undefined:initial;
    if(initial)params.set('operation_id',initial.operation_id);
    try{
      while(!controller.signal.aborted){
        if(!operation){
          const response=await fetch(url+'?'+params,{signal:controller.signal});
          if(response.status===404)return;
          if(!response.ok)throw new Error('Saved comparison is unavailable. Check the selected revisions and try again.');
          operation=await response.json() as ComparisonOperation;
        }
        if(controller.signal.aborted||comparisonController.current!==controller)return;
        operationRef.current={url,id:operation.operation_id};
        setOperationReady(true);
        params.set('operation_id',operation.operation_id);
        setNotice('Comparison '+operation.state+(operation.current_step?' · '+operation.current_step:''));
        if(terminal.has(operation.state)){
          if(operation.state==='COMPLETED'&&operation.result)setResult(operation.result);
          else if(operation.state==='CANCELLED')setNotice('Comparison cancelled.');
          else setError(operation.error||'Comparison did not complete. You can start a new comparison.');
          return;
        }
        setBusy(true);
        await new Promise(resolve=>setTimeout(resolve,500));
        operation=undefined;
      }
    }catch(e){if(!controller.signal.aborted&&comparisonController.current===controller)setError(String(e));}
    finally{if(comparisonController.current===controller){comparisonController.current=null;operationRef.current=null;setOperationReady(false);setBusy(false);}}
  }
  // Reopen the selected immutable pair from server state; navigation only stops polling.
  useEffect(()=>{
    comparisonController.current?.abort();operationRef.current=null;setOperationReady(false);setBusy(false);setResult(null);setError('');setNotice('');
    const row=targets.find(row=>row.id===target);
    if(!base.startsWith('u:')||!profileId||!row?.application_id)return;
    const controller=new AbortController();comparisonController.current=controller;
    void observe(operationUrl(row),new URLSearchParams({base_revision_id:base.slice(2),tailored_document_id:target}),controller);
    return ()=>controller.abort();
  },[profileId,base,target,recovery]);
  if(!targets.length)return <p>Base ↔ tailored comparison needs a generated tailored resume for this application.</p>;
  async function compare(){
    const controller=new AbortController();comparisonController.current=controller;
    setBusy(true);setError('');setNotice('');setResult(null);
    try{
      const row=targets.find(row=>row.id===target);
      if(!row?.application_id)throw new Error('Original application history is unavailable.');
      if(!profileId||row.profile_id!==profileId)throw new Error('Selected resume does not belong to the active profile.');
      const fromUpload=base.startsWith('u:');
      const params:Record<string,string>=fromUpload?{base_revision_id:base.slice(2),tailored_document_id:target}:{base_document_id:base.slice(2),tailored_document_id:target};
      if(fromUpload){
        const url=operationUrl(row);const key=url+'?'+new URLSearchParams(params);
        if(requestRef.current?.key!==key)requestRef.current={key,id:crypto.randomUUID()};
        const admitted=await api<ComparisonOperation>(url+'?'+new URLSearchParams({...params,request_id:requestRef.current.id}),{method:'POST',signal:controller.signal});
        requestRef.current=null;
        await observe(url,new URLSearchParams(params),controller,admitted);
      }else setResult(await api<Comparison>('/profiles/'+encodeURIComponent(profileId)+'/applications/'+encodeURIComponent(row.application_id)+'/document-comparison?'+new URLSearchParams(params),{signal:controller.signal}));
    }catch(e){if(!controller.signal.aborted&&comparisonController.current===controller)setError(String(e));}finally{if(comparisonController.current===controller){comparisonController.current=null;setBusy(false);}}
  }
  async function cancelComparison(){
    const controller=comparisonController.current;
    if(!controller)return;
    const operation=operationRef.current;
    if(base.startsWith('u:')&&operation){
      try{const outcome=await api<ComparisonOperation>(operation.url+'/'+encodeURIComponent(operation.id)+'/cancel',{method:'POST'});
        if(outcome.state==='COMPLETED'){setNotice('Comparison already completed.');return;}
        setNotice(outcome.state==='CANCELLED'?'Comparison cancelled.':'Cancellation requested.');}
      catch(e){setError('Cancellation request failed: '+String(e));return;}
    }else return;
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
    {busy&&base.startsWith('u:')&&<button type="button" disabled={!operationReady} onClick={()=>void cancelComparison()}>Cancel comparison</button>}
    {base.startsWith('u:')&&<><p>Uploaded comparisons run in the background. Reopen these revisions to recover progress or the saved result.</p><button type="button" disabled={busy} onClick={()=>setRecovery(value=>value+1)}>Recover saved comparison</button></>}
    {notice&&<p role="status">{notice}</p>}
    {error&&<p role="alert">Comparison unavailable: {error}</p>}
    {result&&<ComparisonResult result={result}/>}
  </section>;
}
