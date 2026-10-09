import React,{useCallback,useEffect,useMemo,useRef,useState} from 'react';

export type CoverLetterPreviewStyle='professional'|'warm'|'technical'|'direct'|'conversational'|'formal';
export type CoverLetterPreviewLength='VERY_SHORT'|'CONCISE'|'NORMAL'|'DETAILED';
export type ApprovedCoverLetterFact={
  id:string;fact_id?:string;text:string;source:string;verified:boolean;user_approved:boolean;
};
export type CoverLetterPreviewSelection={
  profileId:string;profileVersion:number;jobId:string;jobFingerprint:string;
};
export type CoverLetterPreviewApi=(path:string,init:RequestInit)=>Promise<unknown>;
export type CoverLetterPreviewCompanyContext={
  content_origin:'USER_PROVIDED';content_status:'UNVERIFIED';content:string;source_url:string;
  [key:string]:unknown;
};
export type CoverLetterPreviewResult={
  profile_id:string;profile_version:number;job_id:string;job_fingerprint:string;
  preview_text:string;fact_ids:string[];matched_terms:Record<string,string[]>;
  responsibilities:string[];skills:string[];skill_strengths:Record<string,'REQUIRED'|'PREFERRED'|'UNKNOWN'>;
  company_identity:string;company_context:CoverLetterPreviewCompanyContext|null;
  read_only:true;document_created:false;
};
export type CoverLetterPreviewErrorCode='INVALID_REQUEST'|'INVALID_RESPONSE'|'STALE_SELECTION'|'PREVIEW_FAILED';

export class CoverLetterPreviewError extends Error {
  constructor(readonly code:CoverLetterPreviewErrorCode,message:string){super(message);this.name='CoverLetterPreviewError';}
}

type RecordValue=Record<string,unknown>;
function isRecord(value:unknown):value is RecordValue{return !!value&&typeof value==='object'&&!Array.isArray(value);}
function isIdentifier(value:unknown):value is string{
  return typeof value==='string'&&!!value&&value===value.trim()&&value.length<=128&&
    !/[\\/?#\u0000-\u001f\u007f]/.test(value);
}
function isStringList(value:unknown):value is string[]{
  return Array.isArray(value)&&value.length<=200&&value.every(item=>typeof item==='string'&&!!item.trim()&&item.length<=1000);
}
function validateSelection(selection:CoverLetterPreviewSelection){
  if(!isIdentifier(selection.profileId)||!isIdentifier(selection.jobId)||
    !Number.isSafeInteger(selection.profileVersion)||selection.profileVersion<1||
    !/^[0-9a-f]{64}$/.test(selection.jobFingerprint)){
    throw new CoverLetterPreviewError('INVALID_REQUEST','Select a current profile and saved job before previewing.');
  }
}

export function validateCoverLetterPreview(value:unknown,selection:CoverLetterPreviewSelection):CoverLetterPreviewResult{
  validateSelection(selection);
  if(!isRecord(value))throw new CoverLetterPreviewError('INVALID_RESPONSE','The cover letter preview response is invalid.');
  if(value.profile_id!==selection.profileId||value.job_id!==selection.jobId||
    value.profile_version!==selection.profileVersion||value.job_fingerprint!==selection.jobFingerprint){
    throw new CoverLetterPreviewError('STALE_SELECTION','The selected profile or job changed. Reload the selected profile and job before previewing again.');
  }
  if(typeof value.preview_text!=='string'||!value.preview_text.trim()||value.preview_text.length>20000||
    !isStringList(value.fact_ids)||!value.fact_ids.length||value.fact_ids.length>100||new Set(value.fact_ids).size!==value.fact_ids.length||
    !isRecord(value.matched_terms)||!isStringList(value.responsibilities)||!value.responsibilities.length||
    !isStringList(value.skills)||!value.skills.length||!isRecord(value.skill_strengths)||
    typeof value.company_identity!=='string'||!value.company_identity||value.company_identity.length>128||
    value.read_only!==true||value.document_created!==false){
    throw new CoverLetterPreviewError('INVALID_RESPONSE','The preview was not confirmed as a read-only result. Reload the selected profile and job.');
  }
  const factIds=value.fact_ids as string[];
  const terms=value.matched_terms as RecordValue;
  const responsibilities=value.responsibilities as string[];
  const skills=value.skills as string[];
  const strengths=value.skill_strengths as RecordValue;
  if(factIds.some(id=>!isIdentifier(id))||factIds.some(id=>!isStringList(terms[id])||!(terms[id] as string[]).length)||
    skills.some(skill=>!['REQUIRED','PREFERRED','UNKNOWN'].includes(String(strengths[skill])))){
    throw new CoverLetterPreviewError('INVALID_RESPONSE','Preview evidence or job attribution is incomplete. Reload the selected profile and job.');
  }
  let companyContext:CoverLetterPreviewCompanyContext|null=null;
  if(value.company_context!==null){
    const context=value.company_context;
    if(!isRecord(context)||context.content_origin!=='USER_PROVIDED'||context.content_status!=='UNVERIFIED'||
      typeof context.content!=='string'||!context.content.trim()||context.content.length>4000||
      typeof context.source_url!=='string'||!/^https?:\/\//i.test(context.source_url)||context.source_url.length>2048){
      throw new CoverLetterPreviewError('INVALID_RESPONSE','Company context is missing its user-provided, unverified attribution.');
    }
    companyContext=context as CoverLetterPreviewCompanyContext;
  }
  return {
    profile_id:selection.profileId,profile_version:selection.profileVersion,job_id:selection.jobId,
    job_fingerprint:selection.jobFingerprint,preview_text:value.preview_text,fact_ids:[...factIds],
    matched_terms:Object.fromEntries(factIds.map(id=>[id,[...(terms[id] as string[])]])),
    responsibilities:[...responsibilities],skills:[...skills],
    skill_strengths:Object.fromEntries(skills.map(skill=>[skill,strengths[skill] as 'REQUIRED'|'PREFERRED'|'UNKNOWN'])),
    company_identity:value.company_identity,company_context:companyContext,read_only:true,document_created:false,
  };
}

function isStaleError(error:unknown):boolean{
  return isRecord(error)&&(error.status===409||
    (typeof error.code==='string'&&['PROFILE_VERSION_MISMATCH','PROFILE_CHANGED','TARGET_JOB_CHANGED','JOB_CHANGED','STALE_SELECTION'].includes(error.code)));
}

export function createCoverLetterPreviewClient({api,...selection}:CoverLetterPreviewSelection&{api:CoverLetterPreviewApi}){
  validateSelection(selection);
  return {async generate({style,length}:{style:CoverLetterPreviewStyle;length:CoverLetterPreviewLength}){
    if(!['professional','warm','technical','direct','conversational','formal'].includes(style)||
      !['VERY_SHORT','CONCISE','NORMAL','DETAILED'].includes(length)){
      throw new CoverLetterPreviewError('INVALID_REQUEST','Choose a supported cover letter style and length.');
    }
    const path=`/profiles/${encodeURIComponent(selection.profileId)}/jobs/${encodeURIComponent(selection.jobId)}/cover-letter-preview`;
    let response:unknown;
    try{
      response=await api(path,{method:'POST',headers:{accept:'application/json','content-type':'application/json'},
        body:JSON.stringify({expected_profile_version:selection.profileVersion,
          expected_job_fingerprint:selection.jobFingerprint,style,length})});
    }catch(error){
      if(isStaleError(error))throw new CoverLetterPreviewError('STALE_SELECTION',
        'The selected profile or job changed. Reload the selected profile and job before previewing again.');
      throw new CoverLetterPreviewError('PREVIEW_FAILED','The preview could not be loaded. Check the selected profile and job, then try again.');
    }
    return validateCoverLetterPreview(response,selection);
  }};
}

export function mapApprovedCoverLetterFactSources(preview:CoverLetterPreviewResult,facts:readonly ApprovedCoverLetterFact[]){
  const byId=new Map<string,ApprovedCoverLetterFact[]>();
  for(const fact of facts){const id=fact.fact_id||fact.id;byId.set(id,[...(byId.get(id)||[]),fact]);}
  return preview.fact_ids.map(id=>{
    const matches=byId.get(id)||[],fact=matches.length===1?matches[0]:undefined;
    if(!fact||fact.verified!==true||fact.user_approved!==true||!fact.source?.trim()||!fact.text?.trim()){
      throw new CoverLetterPreviewError('INVALID_RESPONSE','An included fact has no unique verified, user-approved source. Reload approved profile facts.');
    }
    return {id,text:fact.text,source:fact.source,matchedTerms:preview.matched_terms[id]||[]};
  });
}

export function CoverLetterReviewPreviewCard({profileId,jobId,jobTitle,company,approvedFacts,preview}:{
  profileId:string;jobId:string;jobTitle:string;company:string;
  approvedFacts:readonly ApprovedCoverLetterFact[];preview:CoverLetterPreviewResult;
}){
  const facts=mapApprovedCoverLetterFactSources(preview,approvedFacts);
  return <section className="panel" aria-label="Cover letter preview and evidence review">
    <h3>Cover letter preview</h3>
    <p>Selected profile: <strong>{profileId}</strong></p>
    <p>Selected job: <strong>{jobTitle} at {company}</strong> <span>({jobId})</span></p>
    <p role="status">Preview only. No document is saved by preview. Use the existing Prepare flow only when you choose to create a document.</p>
    <section aria-label="Generated cover letter copy"><h4>Generated copy</h4><pre className="document-preview">{preview.preview_text}</pre></section>
    <section aria-label="Approved fact sources"><h4>Approved fact sources</h4><ul>{facts.map(fact=><li key={fact.id}>
      <strong>{fact.id}</strong>: {fact.text}<br/><span>Source: {fact.source}</span>
      {fact.matchedTerms.length>0&&<span> · Matched job terms: {fact.matchedTerms.join(', ')}</span>}
    </li>)}</ul></section>
    <section aria-label="Job responsibility provenance"><h4>Responsibilities from the selected job posting</h4>
      <p>Source: saved job posting {jobId}</p><ul>{preview.responsibilities.map((item,index)=><li key={`${index}:${item}`}>{item}</li>)}</ul>
    </section>
    <section aria-label="Job skills and strength"><h4>Skills from the selected job posting</h4>
      <p>Source: saved job posting {jobId}</p><ul>{preview.skills.map(skill=><li key={skill}>
        {skill} — {preview.skill_strengths[skill]==='REQUIRED'?'Required':preview.skill_strengths[skill]==='PREFERRED'?'Preferred':'Strength not specified'}
      </li>)}</ul>
    </section>
    <section aria-label="Company context attribution"><h4>Company information</h4><p>Company identity: {preview.company_identity}</p>
      {preview.company_context?<><p><strong>USER_PROVIDED / CONTENT UNVERIFIED</strong></p>
        <p>{preview.company_context.content}</p><p>Source: {preview.company_context.source_url}</p>
      </>:<p>No reviewed company context was included in this preview.</p>}
    </section>
    <p className="safety">External submission is not part of preview.</p>
  </section>;
}

export function CoverLetterReviewPreview({profileId,profileVersion,jobId,jobFingerprint,jobTitle,company,approvedFacts,api}:CoverLetterPreviewSelection&{
  jobTitle:string;company:string;approvedFacts:readonly ApprovedCoverLetterFact[];api:CoverLetterPreviewApi;
}){
  const selection=useMemo(()=>({profileId,profileVersion,jobId,jobFingerprint}),[profileId,profileVersion,jobId,jobFingerprint]);
  const client=useMemo(()=>createCoverLetterPreviewClient({api,...selection}),[api,selection]);
  const scope=`${profileId}\u0000${profileVersion}\u0000${jobId}\u0000${jobFingerprint}`;
  const sequence=useRef(0),activeScope=useRef(scope);activeScope.current=scope;
  const [style,setStyle]=useState<CoverLetterPreviewStyle>('professional');
  const [length,setLength]=useState<CoverLetterPreviewLength>('NORMAL');
  const [busy,setBusy]=useState(false);
  const [state,setState]=useState<{scope:string;preview?:CoverLetterPreviewResult;error?:string}|null>(null);
  useEffect(()=>{sequence.current+=1;setBusy(false);setState(null);},[scope]);
  const generate=useCallback(async()=>{
    const call=++sequence.current,requestedScope=scope;setBusy(true);setState(null);
    try{const value=await client.generate({style,length});
      if(sequence.current===call&&activeScope.current===requestedScope)setState({scope:requestedScope,preview:value});
    }catch(error){if(sequence.current===call&&activeScope.current===requestedScope)
      setState({scope:requestedScope,error:error instanceof Error?error.message:'The preview could not be loaded.'});
    }finally{if(sequence.current===call&&activeScope.current===requestedScope)setBusy(false);}
  },[client,length,scope,style]);
  const current=state?.scope===scope?state:null;
  let evidenceError='';
  try{if(current?.preview)mapApprovedCoverLetterFactSources(current.preview,approvedFacts);}
  catch(error){evidenceError=error instanceof Error?error.message:'Approved fact sources could not be verified.';}
  return <section className="panel" aria-label="Cover letter review preview">
    <h2>Review a cover letter preview</h2>
    <p>Selected profile: <strong>{profileId}</strong> · version {profileVersion}</p>
    <p>Selected job: <strong>{jobTitle} at {company}</strong> · {jobId}</p>
    <p>Preview requests are read-only. No document is saved until you choose the existing Prepare flow.</p>
    <div className="toolbar">
      <label>Style<select value={style} disabled={busy} onChange={event=>{setStyle(event.currentTarget.value as CoverLetterPreviewStyle);setState(null);}}>
        <option value="professional">Professional</option><option value="warm">Warm</option><option value="technical">Technical</option>
        <option value="direct">Direct</option><option value="conversational">Conversational</option><option value="formal">Formal</option>
      </select></label>
      <label>Length<select value={length} disabled={busy} onChange={event=>{setLength(event.currentTarget.value as CoverLetterPreviewLength);setState(null);}}>
        <option value="VERY_SHORT">Very short</option><option value="CONCISE">Concise</option><option value="NORMAL">Normal</option><option value="DETAILED">Detailed</option>
      </select></label>
      <button type="button" disabled={busy} onClick={()=>void generate()}>{busy?'Generating preview…':current?.preview?'Refresh preview':'Preview cover letter'}</button>
    </div>
    {busy&&<p role="status">Generating a read-only preview for the selected profile and job…</p>}
    {current?.error&&<div role="alert"><p>{current.error}</p>
      {current.error.includes('Reload the selected profile and job')&&<p>Refresh the selected profile and job before requesting another preview.</p>}
    </div>}
    {evidenceError&&<p role="alert">{evidenceError}</p>}
    {current?.preview&&!evidenceError&&<CoverLetterReviewPreviewCard profileId={profileId} jobId={jobId} jobTitle={jobTitle}
      company={company} approvedFacts={approvedFacts} preview={current.preview}/>}
    <p className="safety">This surface does not save a document or submit an application.</p>
  </section>;
}
