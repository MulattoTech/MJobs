import React, { useEffect, useMemo, useState } from 'react';

export type ApplicationMultipageField={
  id:string;
  label:string;
  required?:boolean;
  type?:'text'|'email'|'tel'|'url'|'textarea';
};

export type ApplicationMultipagePage={
  id:string;
  title:string;
  fields:ApplicationMultipageField[];
};

export type ApplicationMultipageState={
  profile_id:string;
  application_id:string;
  current_page_id:string|null;
  page_order:string[];
  status:'IN_PROGRESS'|'REVIEW';
  progress:Record<string,Record<string,string>>;
  version:number;
  updated_at:string|null;
  manual_intervention_required:boolean;
};

export type ApplicationMultipageAction='NEXT'|'PREVIOUS'|'REVIEW'|'SAVE_PROGRESS';

export type ApplicationMultipageTransition={
  request_id:string;
  action:ApplicationMultipageAction;
  expected_version:number;
  page_id:string;
  page_order?:string[];
  next_page_id?:string;
  previous_page_id?:string;
  progress?:Record<string,Record<string,string>>;
};

export type ApplicationMultipageClient={
  load:(profileId:string,applicationId:string)=>Promise<ApplicationMultipageState>;
  transition:(profileId:string,applicationId:string,request:ApplicationMultipageTransition)=>Promise<ApplicationMultipageState>;
};

export function canContinueApplicationPage(page:ApplicationMultipagePage,values:Record<string,string>):string[] {
  return page.fields.filter(field=>field.required&&!String(values[field.id]||'').trim()).map(field=>field.label);
}

function isMatchingState(value:ApplicationMultipageState,profileId:string,applicationId:string,pages:ApplicationMultipagePage[]):boolean {
  const expectedOrder=pages.map(page=>page.id);
  const uninitialized=value.version===0&&value.current_page_id===null&&Array.isArray(value.page_order)&&value.page_order.length===0;
  const review=value.status==='REVIEW'&&value.current_page_id==='review';
  const knownPage=value.current_page_id!==null&&expectedOrder.includes(value.current_page_id);
  return value.profile_id===profileId&&value.application_id===applicationId
    &&Number.isInteger(value.version)&&value.version>=0
    &&Array.isArray(value.page_order)&&(uninitialized||JSON.stringify(value.page_order)===JSON.stringify(expectedOrder))
    &&(uninitialized||review||knownPage)
    &&(value.status==='IN_PROGRESS'||value.status==='REVIEW')
    &&typeof value.manual_intervention_required==='boolean'
    &&typeof value.progress==='object'&&value.progress!==null&&!Array.isArray(value.progress)
    &&Object.values(value.progress).every(item=>typeof item==='object'&&item!==null&&!Array.isArray(item)
      &&Object.values(item).every(answer=>typeof answer==='string'));
}

async function transition(client:ApplicationMultipageClient,profileId:string,applicationId:string,
  pages:ApplicationMultipagePage[],request:Omit<ApplicationMultipageTransition,'request_id'>):Promise<ApplicationMultipageState> {
  const result=await client.transition(profileId,applicationId,{request_id:crypto.randomUUID(),...request});
  if(!isMatchingState(result,profileId,applicationId,pages))
    throw new Error('The saved progress did not match this profile and application. Reload the review before continuing.');
  return result;
}

export async function saveApplicationMultipageProgress(client:ApplicationMultipageClient,profileId:string,applicationId:string,
  state:ApplicationMultipageState,pages:ApplicationMultipagePage[],progress:Record<string,Record<string,string>>):Promise<ApplicationMultipageState> {
  if(!isMatchingState(state,profileId,applicationId,pages))
    throw new Error('The current progress no longer matches this profile and application. Reload before saving.');
  if(state.status==='REVIEW')throw new Error('Manual review is a stop state. Return to a form page before saving progress.');
  const pageId=state.current_page_id||pages[0]?.id;
  if(!pageId)throw new Error('This application has no configured form pages.');
  return transition(client,profileId,applicationId,pages,{
    action:'SAVE_PROGRESS',expected_version:state.version,page_id:pageId,page_order:pages.map(page=>page.id),progress,
  });
}

export async function moveApplicationMultipageReview(client:ApplicationMultipageClient,profileId:string,applicationId:string,
  state:ApplicationMultipageState,pages:ApplicationMultipagePage[],progress:Record<string,Record<string,string>>,direction:'NEXT'|'PREVIOUS'):
  Promise<ApplicationMultipageState> {
  if(!isMatchingState(state,profileId,applicationId,pages))
    throw new Error('The current progress no longer matches this profile and application. Reload before continuing.');
  if(state.status==='REVIEW'&&state.current_page_id==='review') {
    if(direction==='NEXT')throw new Error('Manual review is the final step. Complete any employer submission yourself.');
    const previousPageId=pages[pages.length-1]?.id;
    if(!previousPageId)throw new Error('There is no prior form page to review.');
    return transition(client,profileId,applicationId,pages,{
      action:'PREVIOUS',expected_version:state.version,page_id:'review',page_order:pages.map(page=>page.id),previous_page_id:previousPageId,
    });
  }
  const currentPageId=state.current_page_id||pages[0]?.id;
  const currentIndex=pages.findIndex(page=>page.id===currentPageId);
  if(currentIndex<0)throw new Error('The saved page is unavailable. Reload this application review.');
  const destinationIndex=currentIndex+(direction==='NEXT'?1:-1);
  if(direction==='PREVIOUS'&&destinationIndex<0)
    throw new Error('There is no previous page in that direction.');
  if(direction==='NEXT') {
    const missing=canContinueApplicationPage(pages[currentIndex],progress[currentPageId]||{});
    if(missing.length)throw new Error(`Complete required fields before continuing: ${missing.join(', ')}.`);
  }
  const saved=await saveApplicationMultipageProgress(client,profileId,applicationId,state,pages,progress);
  const isFinal=direction==='NEXT'&&destinationIndex>=pages.length;
  const request:Omit<ApplicationMultipageTransition,'request_id'>={
    action:isFinal?'REVIEW':direction,expected_version:saved.version,page_id:saved.current_page_id||currentPageId,
    page_order:pages.map(page=>page.id),
    ...(!isFinal&&direction==='NEXT'?{next_page_id:pages[destinationIndex].id}:{}),
    ...(direction==='PREVIOUS'?{previous_page_id:pages[destinationIndex].id}:{}),
  };
  return transition(client,profileId,applicationId,pages,request);
}

export function ApplicationMultipageReviewView({pages,state,draft,pending,error,validationAttempted=false,onFieldChange,onPrevious,onContinue,onSaveProgress,onReload}:
  {pages:ApplicationMultipagePage[];state:ApplicationMultipageState;draft:Record<string,string>;pending:boolean;error:string|null;
    validationAttempted?:boolean;onFieldChange:(fieldId:string,value:string)=>void;onPrevious:()=>void;onContinue:()=>void;onSaveProgress:()=>void;onReload:()=>void}) {
  const isReview=state.status==='REVIEW'||state.current_page_id==='review';
  const currentIndex=isReview?pages.length:Math.max(0,pages.findIndex(page=>page.id===(state.current_page_id||pages[0]?.id)));
  const current=isReview?{id:'review',title:'Review',fields:[]}:pages[currentIndex];
  if(!current)return <section className="panel" aria-label="Multi-page application review"><p role="alert">The saved page is unavailable. Reload this application review.</p></section>;
  const missing=canContinueApplicationPage(current,draft);
  const totalPages=pages.length+1;
  return <section className="panel application-multipage-review" aria-label="Multi-page application review">
    <div className="toolbar"><div><h2>Application review</h2><p>Progress is saved to this profile and application only.</p></div><span className="tag">Page {currentIndex+1} of {totalPages}</span></div>
    <ol aria-label="Application progress" className="application-progress">{[...pages,{id:'review',title:'Review',fields:[]}].map((page,index)=><li key={page.id} aria-current={index===currentIndex?'step':undefined}>
      {index+1}. {page.title}{index===currentIndex?' · Current':''}
    </li>)}</ol>
    <h3>{current.title}</h3>
    {isReview?<div className="fact" role="status"><strong>Manual intervention required</strong>
      <p>Review the employer form yourself and submit it on the employer’s website. MJobs does not submit applications.</p>
    </div>:<div className="application-page-fields">{current.fields.map(field=>{
      const value=draft[field.id]||'';
      const invalid=validationAttempted&&field.required&&!value.trim();
      return <label key={field.id}>
        {field.label}{field.required?<span aria-hidden="true"> *</span>:null}
        {field.type==='textarea'?<textarea aria-label={field.label} required={field.required} aria-invalid={invalid||undefined}
          value={value} disabled={pending} onChange={event=>onFieldChange(field.id,event.currentTarget.value)}/>:<input aria-label={field.label}
          type={field.type||'text'} required={field.required} aria-invalid={invalid||undefined} value={value} disabled={pending}
          onChange={event=>onFieldChange(field.id,event.currentTarget.value)}/>}
        {invalid&&<span role="alert">{field.label} is required.</span>}
      </label>;
    })}</div>}
    {validationAttempted&&missing.length>0&&<p role="alert">Complete required fields before continuing: {missing.join(', ')}.</p>}
    {!validationAttempted&&missing.length>0&&<p className="muted" role="status">Required to continue: {missing.join(', ')}.</p>}
    {error&&<p role="alert">{error}</p>}
    <div className="toolbar">
      <button type="button" disabled={pending||(!isReview&&currentIndex===0)} onClick={onPrevious}>Previous</button>
      {!isReview&&<button type="button" disabled={pending||missing.length>0} onClick={onContinue}>Continue</button>}
      {!isReview&&<button type="button" disabled={pending} onClick={onSaveProgress}>Save progress</button>}
      {error&&<button type="button" disabled={pending} onClick={onReload}>Reload saved progress</button>}
    </div>
    <p className="muted">External submission is unavailable in this review.</p>
  </section>;
}

export function ApplicationMultipageReview({profileId,applicationId,pages,initialState,client}:
  {profileId:string;applicationId:string;pages:ApplicationMultipagePage[];initialState:ApplicationMultipageState;client:ApplicationMultipageClient}) {
  const [state,setState]=useState<ApplicationMultipageState|null>(initialState);
  const initialPageId=initialState.current_page_id||pages[0]?.id;
  const [draft,setDraft]=useState<Record<string,string>>(initialPageId?initialState.progress[initialPageId]||{}:{});
  const [pending,setPending]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [validationAttempted,setValidationAttempted]=useState(false);
  const pageKey=useMemo(()=>pages.map(page=>page.id).join('\u0000'),[pages]);

  useEffect(()=>{
    let active=true;
    setError(null);setValidationAttempted(false);
    if(isMatchingState(initialState,profileId,applicationId,pages)) {
      const pageId=initialState.current_page_id||pages[0]?.id;
      setState(initialState);setDraft(pageId?initialState.progress[pageId]||{}:{});setPending(false);
      return ()=>{active=false;};
    }
    setState(null);setDraft({});setPending(true);
    void client.load(profileId,applicationId).then(result=>{
      if(!isMatchingState(result,profileId,applicationId,pages))
        throw new Error('Saved progress is unavailable for this profile and application.');
      if(!active)return;
      const pageId=result.current_page_id||pages[0]?.id;
      setState(result);setDraft(pageId?result.progress[pageId]||{}:{});
    }).catch(reason=>{
      if(active)setError(reason instanceof Error?reason.message:'Could not load this application review.');
    }).finally(()=>{if(active)setPending(false);});
    return ()=>{active=false;};
  // Refresh local form state when the selected application or configured page sequence changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[profileId,applicationId,pageKey]);

  const reload=async()=>{
    setPending(true);setError(null);
    try {
      const result=await client.load(profileId,applicationId);
      if(!isMatchingState(result,profileId,applicationId,pages))throw new Error('Saved progress is unavailable for this profile and application.');
      const pageId=result.current_page_id||pages[0]?.id;
      const oldPageId=state?.current_page_id||pages[0]?.id;
      setState(result);setDraft({...((pageId&&result.progress[pageId])||{}),...(oldPageId===pageId?draft:{})});
    } catch(reason) {setError(reason instanceof Error?reason.message:'Could not load this application review.');}
    finally {setPending(false);}
  };

  const run=async(operation:()=>Promise<ApplicationMultipageState>)=>{
    if(!state)return;
    setPending(true);setError(null);
    try{const result=await operation();setState(result);setDraft(result.current_page_id&&result.current_page_id!=='review'?result.progress[result.current_page_id]||{}:{});setValidationAttempted(false);}
    catch(reason){setError(reason instanceof Error?reason.message:'Progress could not be saved. Retry after checking the saved version.');}
    finally{setPending(false);}
  };
  if(!state||!isMatchingState(state,profileId,applicationId,pages))return <section className="panel" aria-label="Multi-page application review">
    <h2>Application review</h2>{pending||!error?<p role="status">Loading saved application progress…</p>:<p role="alert">{error||'Saved application progress could not be loaded.'}</p>}
    {!pending&&error&&<button type="button" onClick={()=>void reload()}>Reload progress</button>}
  </section>;

  const currentPageId=state.current_page_id||pages[0]?.id;
  const current=pages.find(page=>page.id===currentPageId);
  const progress={...state.progress,...(currentPageId?{[currentPageId]:draft}:{})};
  return <ApplicationMultipageReviewView pages={pages} state={state} draft={draft} pending={pending} error={error}
    validationAttempted={validationAttempted} onFieldChange={(fieldId,value)=>setDraft(previous=>({...previous,[fieldId]:value}))}
    onPrevious={()=>{if(!state.current_page_id&&state.version===0)return;void run(()=>moveApplicationMultipageReview(client,profileId,applicationId,state,pages,progress,'PREVIOUS'));}}
    onContinue={()=>{
      setValidationAttempted(true);
      if(!current||canContinueApplicationPage(current,draft).length)return;
      void run(()=>moveApplicationMultipageReview(client,profileId,applicationId,state,pages,progress,'NEXT'));
    }}
    onSaveProgress={()=>void run(()=>saveApplicationMultipageProgress(client,profileId,applicationId,state,pages,progress))}
    onReload={()=>void reload()}/>;
}
