import React,{useCallback,useEffect,useState} from 'react';
import {api,eligible} from './api';

type Fact={fact_id:string;kind:string;text:string;source:string;verified:boolean;user_approved:boolean;confidence:number};
type AlignmentPreview={profile_id:string;profile_version:string;job_id:string;job_fingerprint:string;selected_fact_ids:string[];selection_mode:'ALL_ELIGIBLE'|'EXPLICIT_SELECTION';matches:{skills:{term:string;fact_ids:string[]}[];responsibilities:{term:string;fact_ids:string[]}[]};unmatched:{skills:string[];responsibilities:string[]};read_only:true};
export type ResumeFactRewrite={fact_id:string;text:string;reviewed:boolean};
export type ResumeFactChoice={profileId:string;jobId:string;version:string;ids:string[];rewrites:ResumeFactRewrite[]};
export const REWRITABLE_RESUME_FACT_KINDS=['summary','responsibility','experience','accomplishment','project','technology_experience'] as const;
export function resumeFactRewriteLabel(kind:string){
  if(kind==='summary')return 'summary';
  if(kind==='technology_experience')return 'technology experience';
  if(kind==='experience')return 'experience';
  if(kind==='project')return 'project bullet';
  if(kind==='accomplishment')return 'accomplishment bullet';
  return 'bullet';
}

export function ResumeFactSelection({profileId,jobId,jobFingerprint,disabled,onChange,onPreviewReady}:{profileId:string;jobId:string;jobFingerprint:string;disabled:boolean;onChange:(choice:ResumeFactChoice|null)=>void;onPreviewReady:(value:{profileId:string;jobId:string;profileVersion:string;jobFingerprint:string}|null)=>void}) {
  const [choose,setChoose]=useState(false);const [facts,setFacts]=useState<Fact[]>([]);
  const [version,setVersion]=useState('');const [ids,setIds]=useState<string[]>([]);
  const [rewrites,setRewrites]=useState<Record<string,ResumeFactRewrite>>({});
  const [loading,setLoading]=useState(false);const [error,setError]=useState('');const [refresh,setRefresh]=useState(0);
  const [preview,setPreview]=useState<AlignmentPreview|null>(null);const [previewLoading,setPreviewLoading]=useState(false);const [previewError,setPreviewError]=useState('');
  useEffect(()=>{
    setPreview(null);setPreviewError('');onPreviewReady(null);
    const controller=new AbortController();let active=true;
    setLoading(true);setError('');setVersion('');setIds([]);setFacts([]);
    setRewrites({});onChange(choose?{profileId,jobId,version:'',ids:[],rewrites:[]}:null);
    void api<{version:string;facts:Fact[]}>('/profiles/'+encodeURIComponent(profileId),{signal:controller.signal})
      .then(profile=>{if(active){const rows=profile.facts.filter(eligible);const selected=rows.map(row=>row.fact_id);
        setFacts(rows);setVersion(profile.version);setIds(choose?selected:[]);if(choose)onChange({profileId,jobId,version:profile.version,ids:selected,rewrites:[]});}})
      .catch(failure=>{if(active)setError(String(failure));}).finally(()=>{if(active)setLoading(false);});
    return ()=>{active=false;controller.abort();};
  },[choose,profileId,jobId,jobFingerprint,refresh,onChange,onPreviewReady]);
  const requestAlignmentPreview=useCallback(async(signal?:AbortSignal)=>{
    if(!version||!jobFingerprint)throw new Error('Reload this job and its approved facts before previewing.');
    const result=await api<AlignmentPreview>('/profiles/'+encodeURIComponent(profileId)+'/jobs/'+encodeURIComponent(jobId)+'/resume-alignment-preview',{
      method:'POST',signal,body:JSON.stringify({expected_profile_version:version,expected_job_fingerprint:jobFingerprint,selected_fact_ids:choose?ids:null})});
    const expectedFactIds=choose?facts.filter(fact=>ids.includes(fact.fact_id)).map(fact=>fact.fact_id):facts.map(fact=>fact.fact_id);
    if(result.profile_id!==profileId||result.job_id!==jobId||result.profile_version!==version||result.job_fingerprint!==jobFingerprint||result.read_only!==true||result.selection_mode!==(choose?'EXPLICIT_SELECTION':'ALL_ELIGIBLE')||JSON.stringify(result.selected_fact_ids)!==JSON.stringify(expectedFactIds))throw new Error('Alignment preview snapshot did not match the selected facts, profile and job. Reload them and retry.');
    return result;
  },[version,jobFingerprint,choose,ids,facts,profileId,jobId]);
  useEffect(()=>{
    if(!version||!jobFingerprint)return;
    const controller=new AbortController();let active=true;
    setPreview(null);setPreviewError('');setPreviewLoading(true);onPreviewReady(null);
    void requestAlignmentPreview(controller.signal)
      .then(result=>{if(!active)return;
        setPreview(result);onPreviewReady({profileId,jobId,profileVersion:version,jobFingerprint});})
      .catch(failure=>{if(active)setPreviewError(String(failure));})
      .finally(()=>{if(active)setPreviewLoading(false);});
    return ()=>{active=false;controller.abort();};
  },[version,profileId,jobId,jobFingerprint,refresh,choose,ids,facts,requestAlignmentPreview,onPreviewReady]);
  async function previewAlignment(){
    setPreviewLoading(true);setPreviewError('');setPreview(null);onPreviewReady(null);
    try{
      const result=await requestAlignmentPreview();
      setPreview(result);onPreviewReady({profileId,jobId,profileVersion:version,jobFingerprint});
    }catch(failure){setPreviewError(String(failure));}
    finally{setPreviewLoading(false);}
  }
  function select(id:string,checked:boolean){
    const next=checked?[...ids,id]:ids.filter(value=>value!==id);setIds(next);
    const remaining=checked?rewrites:Object.fromEntries(Object.entries(rewrites).filter(([key])=>key!==id));
    if(!checked)setRewrites(remaining);
    onChange({profileId,jobId,version,ids:next,rewrites:Object.values(remaining).filter(item=>item.text.trim())});
  }
  function editRewrite(fact:Fact,text:string){
    const next={...rewrites,[fact.fact_id]:{fact_id:fact.fact_id,text,reviewed:false}};
    setRewrites(next);onChange({profileId,jobId,version,ids,rewrites:Object.values(next).filter(item=>item.text.trim())});
  }
  function reviewRewrite(fact:Fact,reviewed:boolean){
    const current=rewrites[fact.fact_id];if(!current)return;
    const next={...rewrites,[fact.fact_id]:{...current,reviewed}};
    setRewrites(next);onChange({profileId,jobId,version,ids,rewrites:Object.values(next).filter(item=>item.text.trim())});
  }
  return <section aria-label="Resume content selection">
    <label className="consent"><input type="checkbox" checked={choose} disabled={disabled} onChange={event=>{setChoose(event.target.checked);onChange(event.target.checked?{profileId,jobId,version:'',ids:[],rewrites:[]}:null);}}/> Choose resume facts</label>
    <>
    {choose&&<>
      <p>Select the approved content to include in this resume. Your profile and cover letter keep their approved facts.</p>
      <button type="button" disabled={disabled||loading} onClick={()=>setRefresh(value=>value+1)}>Reload approved facts</button>
      {loading&&<p role="status">Loading saved approved facts…</p>}{error&&<p role="alert">{error}</p>}
      {!loading&&!error&&<><p>{ids.length} of {facts.length} approved facts selected. Choose at least one.</p>
        {facts.map(fact=><div key={fact.fact_id}>
          <label className="consent"><input type="checkbox" aria-label={'Include '+fact.text} checked={ids.includes(fact.fact_id)} disabled={disabled} onChange={event=>select(fact.fact_id,event.target.checked)}/><span>{fact.kind}: {fact.text}</span></label>
          {ids.includes(fact.fact_id)&&REWRITABLE_RESUME_FACT_KINDS.includes(fact.kind as typeof REWRITABLE_RESUME_FACT_KINDS[number])&&<div className="form-row">
            <label>Rewrite this {resumeFactRewriteLabel(fact.kind)} for this application<textarea aria-label={'Rewrite this '+resumeFactRewriteLabel(fact.kind)+': '+fact.text} value={rewrites[fact.fact_id]?.text||''} disabled={disabled} maxLength={10000} onChange={event=>editRewrite(fact,event.target.value)}/></label>
            {Boolean(rewrites[fact.fact_id]?.text.trim())&&<label className="consent"><input type="checkbox" aria-label={'I reviewed this rewrite: '+fact.text} checked={rewrites[fact.fact_id]?.reviewed||false} disabled={disabled} onChange={event=>reviewRewrite(fact,event.target.checked)}/>I reviewed this wording and confirm it preserves the selected approved fact.</label>}
          </div>}
        </div>)}
        {!facts.length&&<p>Save at least one sourced, verified and approved fact with confidence of 80% or higher in Profile.</p>}</>}
    </>}
      <section aria-label="Resume keyword alignment preview">
        <h3>Resume keyword alignment</h3>
        <p>Exact matches use only saved job terms and currently approved profile facts. Unmatched terms stay out of resume content. This preview does not create a document.</p>
        <button type="button" disabled={disabled||loading||previewLoading||!version||!jobFingerprint} onClick={()=>void previewAlignment()}>{previewLoading?'Checking alignment…':'Preview keyword alignment'}</button>
        {previewError&&<p role="alert">{previewError}</p>}
        {preview&&<>
          <p role="status">Read-only preview for this saved job and profile version. {preview.selection_mode==='EXPLICIT_SELECTION'?'Selected resume facts only.':'All eligible approved facts are in the default resume selection.'}</p>
          {(['skills','responsibilities'] as const).map(group=><section key={group} aria-label={group==='skills'?'Skill alignment':'Responsibility alignment'}>
            <h4>{group==='skills'?'Skills':'Responsibilities'}</h4>
            {preview.matches[group].length?<ul>{preview.matches[group].map(row=><li key={row.term}><strong>{row.term}</strong> — {row.fact_ids.length?<>supported by approved fact references: {row.fact_ids.join(', ')}</>:'no eligible approved fact match'}</li>)}</ul>:<p>None supported in the saved job snapshot.</p>}
            {preview.unmatched[group].length>0&&<p><strong>Unmatched {group}:</strong> {preview.unmatched[group].join('; ')}</p>}
          </section>)}
        </>}
      </section>
    </>
  </section>;
}
