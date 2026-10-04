import React,{useEffect,useState} from 'react';
import {api,eligible} from './api';

type Fact={fact_id:string;kind:string;text:string;source:string;verified:boolean;user_approved:boolean;confidence:number};
export type ResumeFactChoice={profileId:string;jobId:string;version:string;ids:string[]};

export function ResumeFactSelection({profileId,jobId,disabled,onChange}:{profileId:string;jobId:string;disabled:boolean;onChange:(choice:ResumeFactChoice|null)=>void}) {
  const [choose,setChoose]=useState(false);const [facts,setFacts]=useState<Fact[]>([]);
  const [version,setVersion]=useState('');const [ids,setIds]=useState<string[]>([]);
  const [loading,setLoading]=useState(false);const [error,setError]=useState('');const [refresh,setRefresh]=useState(0);
  useEffect(()=>{
    if(!choose){onChange(null);return;}
    const controller=new AbortController();let active=true;
    setLoading(true);setError('');setVersion('');setIds([]);setFacts([]);
    onChange({profileId,jobId,version:'',ids:[]});
    void api<{version:string;facts:Fact[]}>('/profiles/'+encodeURIComponent(profileId),{signal:controller.signal})
      .then(profile=>{if(active){const rows=profile.facts.filter(eligible);const selected=rows.map(row=>row.fact_id);
        setFacts(rows);setVersion(profile.version);setIds(selected);onChange({profileId,jobId,version:profile.version,ids:selected});}})
      .catch(failure=>{if(active)setError(String(failure));}).finally(()=>{if(active)setLoading(false);});
    return ()=>{active=false;controller.abort();};
  },[choose,profileId,jobId,refresh,onChange]);
  function select(id:string,checked:boolean){
    const next=checked?[...ids,id]:ids.filter(value=>value!==id);setIds(next);
    onChange({profileId,jobId,version,ids:next});
  }
  return <section aria-label="Resume content selection">
    <label className="consent"><input type="checkbox" checked={choose} disabled={disabled} onChange={event=>{setChoose(event.target.checked);onChange(event.target.checked?{profileId,jobId,version:'',ids:[]}:null);}}/> Choose resume facts</label>
    {choose&&<>
      <p>Select the approved content to include in this resume. Your profile and cover letter keep their approved facts.</p>
      <button type="button" disabled={disabled||loading} onClick={()=>setRefresh(value=>value+1)}>Reload approved facts</button>
      {loading&&<p role="status">Loading saved approved facts…</p>}{error&&<p role="alert">{error}</p>}
      {!loading&&!error&&<><p>{ids.length} of {facts.length} approved facts selected. Choose at least one.</p>
        {facts.map(fact=><label className="consent" key={fact.fact_id}><input type="checkbox" aria-label={'Include '+fact.text} checked={ids.includes(fact.fact_id)} disabled={disabled} onChange={event=>select(fact.fact_id,event.target.checked)}/><span>{fact.kind}: {fact.text}</span></label>)}
        {!facts.length&&<p>Save at least one sourced, verified and approved fact with confidence of 80% or higher in Profile.</p>}</>}
    </>}
  </section>;
}
