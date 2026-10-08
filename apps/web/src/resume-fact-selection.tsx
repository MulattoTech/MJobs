import React,{useEffect,useState} from 'react';
import {api,eligible} from './api';

type Fact={fact_id:string;kind:string;text:string;source:string;verified:boolean;user_approved:boolean;confidence:number};
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

export function ResumeFactSelection({profileId,jobId,disabled,onChange}:{profileId:string;jobId:string;disabled:boolean;onChange:(choice:ResumeFactChoice|null)=>void}) {
  const [choose,setChoose]=useState(false);const [facts,setFacts]=useState<Fact[]>([]);
  const [version,setVersion]=useState('');const [ids,setIds]=useState<string[]>([]);
  const [rewrites,setRewrites]=useState<Record<string,ResumeFactRewrite>>({});
  const [loading,setLoading]=useState(false);const [error,setError]=useState('');const [refresh,setRefresh]=useState(0);
  useEffect(()=>{
    if(!choose){setRewrites({});onChange(null);return;}
    const controller=new AbortController();let active=true;
    setLoading(true);setError('');setVersion('');setIds([]);setFacts([]);
    setRewrites({});onChange({profileId,jobId,version:'',ids:[],rewrites:[]});
    void api<{version:string;facts:Fact[]}>('/profiles/'+encodeURIComponent(profileId),{signal:controller.signal})
      .then(profile=>{if(active){const rows=profile.facts.filter(eligible);const selected=rows.map(row=>row.fact_id);
        setFacts(rows);setVersion(profile.version);setIds(selected);onChange({profileId,jobId,version:profile.version,ids:selected,rewrites:[]});}})
      .catch(failure=>{if(active)setError(String(failure));}).finally(()=>{if(active)setLoading(false);});
    return ()=>{active=false;controller.abort();};
  },[choose,profileId,jobId,refresh,onChange]);
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
  </section>;
}
