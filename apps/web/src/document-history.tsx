import React from 'react';
import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {ResumeComparison} from './resume-comparison';

export type GeneratedDocument = {
  id:string;application_id?:string;profile_id:string;kind:string;version:number;profile_version:string;fact_ids:string[];
  files:Record<string,string>;sha256:Record<string,string>;template:string;
  preview_text?:string|null;preview_error?:string|null;avoid_list_error?:string|null;
  legacy_job_context_missing?:boolean;
  responsibility_context_valid?:boolean;
  story_citations?:{item_index:number;story_id:string;revision:number;fact_provenance:{fact_id:string;fingerprint:string}[]}[];
  created_at:string|null;generation_model:string|null;
  facts_used:{fact_id:string;kind:string;text:string;source:string;confidence:number;verified:boolean;user_approved:boolean}[];
  missing_fact_ids:string[];
  generation_context?:{mode:string;profile_version?:string;job_title?:string;job_skills?:string[];matched_skills?:Record<string,string[]>;original_fact_ids?:string[];ordered_fact_ids?:string[];selection_mode?:string;selected_fact_ids?:string[];omitted_fact_ids?:string[];emphasized_fact_ids?:string[];contact_header?:string[];fact_rephrases?:{fact_id:string;text:string;reviewed:boolean;review_policy:string}[];bullet_adjustment?:{mode?:string;matched_responsibilities?:Record<string,string[]>;selected_reasons?:Record<string,string[]>};role_lens?:{id:string;version:number;display_name:string;selection_reason:'EXPLICIT'|'ROLE_MATCH'|'GENERAL_FALLBACK';fact_ids:string[];emphasized_fact_ids:string[];story_ids:string[];emphasized_story_ids:string[];document_fact_ids?:string[];document_emphasized_fact_ids?:string[]};style?:string;length?:string;skills?:string[];skill_strengths?:Record<string,string>;responsibilities?:string[];company_identity?:string;items?:{question:string;answer_id?:string|null;source:string;confidence:number|null;answer_mode:string}[]}|null;
};

type CoverLetterPreview = Pick<GeneratedDocument,'id'|'application_id'|'profile_id'|'kind'|'version'|'preview_text'|'preview_error'|'avoid_list_error'>;
export type CoverLetterLineChange={kind:'Added'|'Removed';line:string};

export function coverLetterLineDiff(beforeText:string,afterText:string):CoverLetterLineChange[] {
  const before=beforeText.split(/\r?\n/);const after=afterText.split(/\r?\n/);
  const width=after.length+1;
  const lengths=Array.from({length:before.length+1},()=>new Uint32Array(width));
  for(let i=before.length-1;i>=0;i--){
    for(let j=after.length-1;j>=0;j--){
      lengths[i]![j]=before[i]===after[j]?lengths[i+1]![j+1]!+1:Math.max(lengths[i+1]![j]!,lengths[i]![j+1]!);
    }
  }
  const changes:CoverLetterLineChange[]=[];let i=0;let j=0;
  while(i<before.length||j<after.length){
    if(i<before.length&&j<after.length&&before[i]===after[j]){i++;j++;}
    else if(j<after.length&&(i===before.length||lengths[i]![j+1]!>lengths[i+1]![j]!)){
      changes.push({kind:'Added',line:after[j]!});j++;
    }else{
      changes.push({kind:'Removed',line:before[i]!});i++;
    }
  }
  return changes;
}

function CoverLetterComparison({documents}:{documents:GeneratedDocument[]}) {
  const [earlierId,setEarlierId]=useState('');const [laterId,setLaterId]=useState('');
  const [result,setResult]=useState<{earlier:CoverLetterPreview;later:CoverLetterPreview}|null>(null);
  const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const letters=documents.filter(row=>row.kind==='cover-letter'&&row.application_id&&row.profile_id);
  const applicationIds=Array.from(new Set(letters.map(row=>row.application_id)));
  const requestScope=applicationIds.length===1?`${applicationIds[0]}|${letters[0]?.profile_id||''}`:'';
  const latestScope=useRef(requestScope);latestScope.current=requestScope;
  const requestSequence=useRef(0);
  useEffect(()=>()=>{requestSequence.current+=1;},[requestScope]);
  if(applicationIds.length!==1)return applicationIds.length>1
    ?<section aria-label="Compare cover-letter revisions"><h3>Compare cover-letter revisions</h3><p>Open one application’s document history to compare revisions from that application.</p></section>
    :null;
  const applicationId=applicationIds[0]!;
  const profileId=letters[0]!.profile_id;
  const sameApplication=letters.filter(row=>row.application_id===applicationId&&row.profile_id===profileId)
    .sort((a,b)=>a.version-b.version);
  if(sameApplication.length<2)return null;
  const earlier= sameApplication.find(row=>row.id===earlierId)||sameApplication[0]!;
  const later= sameApplication.find(row=>row.id===laterId)||sameApplication[sameApplication.length-1]!;
  async function compare(){
    const currentRequest=++requestSequence.current;
    setBusy(true);setError('');setResult(null);
    try{
      if(earlier.id===later.id)throw new Error('Choose two different cover-letter revisions.');
      if(earlier.application_id!==later.application_id||earlier.application_id!==applicationId||earlier.profile_id!==later.profile_id||earlier.profile_id!==profileId)
        throw new Error('Cover-letter revisions must belong to the same application and profile.');
      const response=await api<{documents:CoverLetterPreview[]}>(`/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/documents`);
      if(requestSequence.current!==currentRequest||latestScope.current!==requestScope)return;
      const current=new Map(response.documents.map(row=>[row.id,row]));
      const validate=(id:string):CoverLetterPreview=>{
        const row=current.get(id);
        if(!row||row.kind!=='cover-letter'||row.application_id!==applicationId||row.profile_id!==profileId)
          throw new Error('A selected cover-letter revision is no longer available for this application.');
        if(row.preview_error||row.avoid_list_error||typeof row.preview_text!=='string'||!row.preview_text.trim())
          throw new Error(row.preview_error||row.avoid_list_error||`Cover-letter version ${row.version} has no validated preview.`);
        return row;
      };
      setResult({earlier:validate(earlier.id),later:validate(later.id)});
    }catch(e){if(requestSequence.current===currentRequest&&latestScope.current===requestScope)setError(String(e));}
    finally{if(requestSequence.current===currentRequest&&latestScope.current===requestScope)setBusy(false);}
  }
  const changes=result?coverLetterLineDiff(result.earlier.preview_text!,result.later.preview_text!):[];
  return <section aria-label="Compare cover-letter revisions">
    <h3>Compare cover-letter revisions</h3>
    <p>Choose two immutable versions from this application. Comparison uses only previews validated by the application document history endpoint.</p>
    <div className="two-col">
      <label>Earlier cover-letter version<select aria-label="Earlier cover-letter version" value={earlier.id} disabled={busy} onChange={event=>{setEarlierId(event.target.value);setResult(null);setError('');}}>{sameApplication.map(row=><option key={row.id} value={row.id}>Version {row.version}</option>)}</select></label>
      <label>Later cover-letter version<select aria-label="Later cover-letter version" value={later.id} disabled={busy} onChange={event=>{setLaterId(event.target.value);setResult(null);setError('');}}>{sameApplication.map(row=><option key={row.id} value={row.id}>Version {row.version}</option>)}</select></label>
    </div>
    <button type="button" disabled={busy||earlier.id===later.id} onClick={()=>void compare()}>{busy?'Loading validated previews…':'Compare selected cover letters'}</button>
    {error&&<p role="alert">Cover-letter comparison unavailable: {error}</p>}
    {result&&<section aria-label="Cover-letter comparison result"><p>Version {result.earlier.version} and version {result.later.version} are immutable saved revisions. Text below comes from the validated application history previews.</p><section aria-label="Cover-letter changes"><h4>Changed lines</h4>{changes.length?<ul>{changes.map((change,index)=><li key={`${change.kind}-${index}`}><strong>{change.kind}</strong><pre>{change.line}</pre></li>)}</ul>:<p>No text changes between these revisions.</p>}</section><div className="resume-comparison-texts">
      <section><h4>Cover letter · v{result.earlier.version}</h4><pre>{result.earlier.preview_text}</pre></section>
      <section><h4>Cover letter · v{result.later.version}</h4><pre>{result.later.preview_text}</pre></section>
    </div></section>}
  </section>;
}

export function DocumentHistory({documents}:{documents:GeneratedDocument[]}) {
  const comparisonScope=documents.map(row=>`${row.profile_id}/${row.application_id||''}`).join('|');
  return <><ResumeComparison key={documents.map(row=>row.id).join('|')} documents={documents}/><CoverLetterComparison key={comparisonScope} documents={documents}/>{documents.map(document=><section className="document" key={document.id} aria-label={`${document.kind} version ${document.version}`}>
    <strong>{document.kind} · v{document.version}</strong>
    {document.kind==='cover-letter'&&document.generation_context?.mode==='JOB_SPECIFIC_APPROVED_FACTS'&&<p>Job-specific deterministic draft · {document.generation_context.length} · {document.generation_context.style}. Posting skill evidence: {document.generation_context.skills?.map(skill=>`${skill} (${document.generation_context?.skill_strengths?.[skill]?.replaceAll('_',' ').toLowerCase()||'unknown'})`).join(', ')||'none recorded'}. Employer identity: {document.generation_context.company_identity==='USER_ENTERED_UNVERIFIED'?'entered manually; verify against the source':'source listed'}.</p>}
    {document.kind==='application-answer-export'&&document.generation_context?.mode==='REVIEWED_APPLICATION_ANSWERS'&&<><p>Manually reviewed application answers · profile snapshot {document.generation_context.profile_version?.slice(0,10)}. This export does not fill or submit an employer form.</p><ul>{document.generation_context.items?.map((item,index)=><li key={`${item.question}-${index}`}>{item.question} · {item.answer_mode==='REVIEWED_REUSE'?'reused with explicit review':'entered for this application'} · {item.source}{item.confidence!==null?` · ${item.confidence}% confidence`:''}</li>)}</ul>{document.story_citations?.length?<section aria-label="Saved story citations"><h4>Reviewed story citations</h4><ul>{document.story_citations.map(citation=><li key={`${citation.item_index}:${citation.story_id}:${citation.revision}`}>Answer {citation.item_index+1} · story revision {citation.story_id} v{citation.revision} · source facts {citation.fact_provenance.map(fact=>fact.fact_id).join(', ')}</li>)}</ul><p>These provenance references were saved separately from the exported answer text.</p></section>:null}</>}
    {document.kind==='cover-letter'&&document.legacy_job_context_missing&&<p>Legacy letter: its original title is preserved from the saved document; the current job title may differ.</p>}
    {document.generation_context?.mode==='APPROVED_SKILL_ORDER'&&(document.generation_context.selection_mode==='EXPLICIT_APPROVED_FACTS'
      ?<p>Tailored resume: selected facts for {document.generation_context.job_title}. Approved facts included: {document.generation_context.selected_fact_ids?.length}. Omitted from this resume: {document.generation_context.omitted_fact_ids?.length}. {document.generation_context.fact_rephrases?.length?`${document.generation_context.fact_rephrases.length} bullet rewrite(s) were entered and reviewed for this application; profile facts and cover letter stay unchanged.`:'Approved wording preserved.'}</p>
      :<p>Tailored resume: skill order for {document.generation_context.job_title}. Wording and work history preserved.</p>)}
    {document.generation_context?.role_lens&&<p>Role lens: {document.generation_context.role_lens.display_name} · v{document.generation_context.role_lens.version} · {document.generation_context.role_lens.selection_reason==='EXPLICIT'?'explicitly selected for this job':document.generation_context.role_lens.selection_reason==='ROLE_MATCH'?'suggested by job title and accepted by the preparation request':'general fallback accepted by the preparation request'}. It referenced {document.generation_context.role_lens.fact_ids.length} approved facts and {document.generation_context.role_lens.story_ids.length} approved {document.generation_context.role_lens.story_ids.length===1?'story':'stories'}; the document records {document.generation_context.role_lens.document_fact_ids?.length??document.fact_ids.length} included facts.</p>}
    <p>{document.fact_ids.length} factual sources · profile snapshot {document.profile_version.slice(0,10)}</p>
    {document.generation_context?.emphasized_fact_ids&&<p>Job-matched approved facts in bold: {document.generation_context.emphasized_fact_ids.length}. DOCX and PDF preserve the same approved text as TXT.</p>}
    {document.kind==='cover-letter'&&document.preview_error&&<p role="alert" className="safety">{document.preview_error} Downloads are disabled until a valid version is prepared.</p>}
    {document.avoid_list_error&&<p role="alert" className="safety">{document.avoid_list_error}. Downloads are disabled until you prepare content that follows the current avoid list.</p>}
    {document.kind==='cover-letter'&&document.preview_text&&<details><summary>Preview validated cover letter</summary><pre className="description" style={{whiteSpace:'pre-wrap',fontFamily:'inherit'}}>{document.preview_text}</pre></details>}
    {(['pdf','docx','txt'] as const).filter(format=>document.files[format]&&!(document.kind==='cover-letter'&&document.preview_error)&&!document.avoid_list_error).map(format=><a key={format} className="button-link" href={'/profiles/'+encodeURIComponent(document.profile_id)+'/documents/'+encodeURIComponent(document.id)+'/'+format}>Download {format.toUpperCase()}</a>)}
    <details><summary>Generation details and facts</summary>
      <p>Generated: {document.created_at ? new Date(document.created_at).toLocaleString() : 'Not recorded for this historical document'}</p>
      <p>Generation method: {document.generation_model || 'Not recorded for this historical document'}</p>
      <p>Template: {document.template}</p>
      <p>Profile snapshot: <code>{document.profile_version}</code></p>
      {document.generation_context?.contact_header&&<section aria-label="Saved resume contact header" style={{overflowWrap:'anywhere'}}><h4>Saved contact details included</h4>{document.generation_context.contact_header.map((line,index)=><p key={index}>{line}</p>)}</section>}
      {document.generation_context?.mode==='APPROVED_SKILL_ORDER'&&<p>Target skill snapshot: {document.generation_context.job_skills?.join(', ')||'No skills detected'}. Exact matches guide skill ordering and fact emphasis; they are not a qualification assessment.</p>}
      {document.missing_fact_ids.length>0&&<p className="alert">Some original facts are unavailable: {document.missing_fact_ids.join(', ')}</p>}
      <ul>{document.facts_used.map(fact=><li key={fact.fact_id}><strong>{fact.kind}</strong>: {fact.text}<br/>Source: {fact.source}<br/>
        At generation: {fact.verified?'verified':'unverified'}, {fact.user_approved?'approved':'not approved'}, confidence {fact.confidence}%
        {!!document.generation_context?.fact_rephrases?.find(item=>item.fact_id===fact.fact_id)&&<p>User-reviewed wording for this application: {document.generation_context.fact_rephrases.find(item=>item.fact_id===fact.fact_id)?.text}</p>}
        {!!document.generation_context?.matched_skills?.[fact.fact_id]?.length&&<p>Matched job skills: {document.generation_context.matched_skills[fact.fact_id].join(', ')}</p>}
        {document.responsibility_context_valid!==false&&document.generation_context?.bullet_adjustment?.selected_reasons?.[fact.fact_id]?.includes('EXACT_TARGET_RESPONSIBILITY_PHRASE')&&!!document.generation_context.bullet_adjustment.matched_responsibilities?.[fact.fact_id]?.length&&<p>Selected for this job responsibility: {document.generation_context.bullet_adjustment.matched_responsibilities[fact.fact_id].join('; ')}</p>}
        {document.generation_context?.emphasized_fact_ids?.includes(fact.fact_id)&&<p>Emphasized in DOCX and PDF.</p>}</li>)}</ul>
      <p>Checksums (SHA-256)</p><ul>{Object.entries(document.sha256).map(([format,hash])=><li key={format}>{format.toUpperCase()}: <code style={{overflowWrap:'anywhere'}}>{hash}</code></li>)}</ul>
    </details>
  </section>)}</>;
}
