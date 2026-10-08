import React, { useState } from 'react';

export type ResumePageFitPageMeasurement = {
  page_target:number;
  page_count:number|null;
  attempts:number;
  measurement_available:boolean;
  target_met:boolean;
  reason:string|null;
  omitted_facts:{fact_id:string;reason:string}[];
};

export type ResumePageFitPreview = {
  preview_id:string;
  profile_id:string;
  job_id:string;
  actual_page_count:number|null;
  unmet_target:boolean;
  measurement_status:'AVAILABLE'|'UNAVAILABLE';
  preview_pdf_url:string;
  generation_context:{page_fit:ResumePageFitPageMeasurement};
};

export type ResumePageFitGenerated = {
  profile_id:string;
  job_id:string;
  preview_id:string;
  application_id:string;
  submit:false;
  status:'PREPARED';
};

function previewPageTarget(preview:ResumePageFitPreview|null|undefined):number|null {
  const target=preview?.generation_context?.page_fit?.page_target;
  return Number.isInteger(target)&&target!>=1&&target!<=20?target!:null;
}

function hasAvailableMeasurement(preview:ResumePageFitPreview|null|undefined):boolean {
  const measurement=preview?.generation_context?.page_fit;
  return preview?.measurement_status==='AVAILABLE'&&measurement?.measurement_available===true
    &&Number.isInteger(preview.actual_page_count)&&preview.actual_page_count!>0
    &&measurement.page_count===preview.actual_page_count;
}

export function canGeneratePageFit(preview:ResumePageFitPreview|null|undefined,profileId:string,jobId:string,targetPages:number,reviewed:boolean):boolean {
  return !!preview&&reviewed&&!!profileId&&!!jobId&&preview.profile_id===profileId&&preview.job_id===jobId
    &&Number.isInteger(targetPages)&&targetPages>=1&&targetPages<=20
    &&previewPageTarget(preview)===targetPages&&hasAvailableMeasurement(preview);
}

export function ResumePageFit({profileId,jobId,preview,generated,busy=false,error='',onPreview,onGenerate}:{
  profileId:string;
  jobId:string;
  preview?:ResumePageFitPreview|null;
  generated?:ResumePageFitGenerated|null;
  busy?:boolean;
  error?:string;
  onPreview:(targetPages:number)=>void;
  onGenerate:(previewId:string)=>void;
}) {
  const [targetPages,setTargetPages]=useState(1);
  const [reviewedPreviewId,setReviewedPreviewId]=useState('');
  const hasScope=!!profileId&&!!jobId;
  const currentPreview=preview?.profile_id===profileId&&preview?.job_id===jobId?preview:null;
  const matchingPreview=previewPageTarget(currentPreview)===targetPages?currentPreview:null;
  const measured=hasAvailableMeasurement(matchingPreview);
  const pageFit=matchingPreview?.generation_context.page_fit;
  const reviewed=!!matchingPreview&&reviewedPreviewId===matchingPreview.preview_id;
  const canGenerate=canGeneratePageFit(matchingPreview,profileId,jobId,targetPages,reviewed);
  const currentGenerated=generated?.profile_id===profileId&&generated?.job_id===jobId?generated:null;

  return <section className="panel" aria-label="Resume PDF page fit">
    <h2>Fit a tailored resume to a PDF page target</h2>
    <p>Review a measured PDF preview before creating a new resume version. Page counts refer to the generated PDF; DOCX pagination is not measured.</p>
    {!hasScope?<p>Select a profile and job to preview page fit.</p>:<>
      <form onSubmit={event=>{event.preventDefault();setReviewedPreviewId('');onPreview(targetPages);}}>
        <label>Target PDF pages<input aria-label="Target PDF pages" type="number" min={1} max={20} step={1} value={targetPages} disabled={busy} onChange={event=>{setTargetPages(Number(event.target.value));setReviewedPreviewId('');}}/></label>
        <button type="submit" disabled={busy||!Number.isInteger(targetPages)||targetPages<1||targetPages>20}>Preview page fit</button>
      </form>
      {matchingPreview&&<div className="fact" role="region" aria-label="Measured PDF page-fit preview">
        <h3>Preview for target: {pageFit!.page_target} {pageFit!.page_target===1?'page':'pages'}</h3>
        {matchingPreview.preview_pdf_url&&<p><a href={matchingPreview.preview_pdf_url} target="_blank" rel="noreferrer">Open measured PDF preview</a></p>}
        {measured?<p>Measured PDF pages: {matchingPreview.actual_page_count}</p>:<><p>PDF page count unavailable</p><p>The page target could not be measured; no page count is estimated.</p></>}
        {measured&&(matchingPreview.unmet_target||!pageFit!.target_met)
          ?<p role="status">The measured PDF does not meet the requested page target.</p>
          :measured?<p role="status">The measured PDF meets the requested page target.</p>:null}
        <p>Render attempts: {pageFit!.attempts}. Attempts are bounded.</p>
        {pageFit!.omitted_facts.length>0?<><p>Approved facts omitted from this tailored preview (the source profile remains unchanged):</p><ul aria-label="Omitted approved facts">{pageFit!.omitted_facts.map(item=><li key={item.fact_id}><code>{item.fact_id}</code> · {item.reason}</li>)}</ul></>:<p>No approved facts were omitted from this preview.</p>}
        {measured&&<label className="consent"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event=>setReviewedPreviewId(event.target.checked?matchingPreview.preview_id:'')}/> I reviewed the measured PDF page-fit preview and omissions</label>}
        <button type="button" disabled={busy||!canGenerate} onClick={()=>onGenerate(matchingPreview.preview_id)}>Generate reviewed PDF</button>
      </div>}
      {currentGenerated&&<div role="status" aria-label="Generated page-fit PDF">
        <h3>Prepared resume version</h3>
        <p>Resume preparation is complete. No application was submitted.</p>
        <p>Preparation reference: <code>{currentGenerated.application_id}</code></p>
      </div>}
    </>}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
