import React from 'react';
import {ResumeComparison} from './resume-comparison';

export type GeneratedDocument = {
  id:string;application_id?:string;profile_id?:string;kind:string;version:number;profile_version:string;fact_ids:string[];
  files:Record<string,string>;sha256:Record<string,string>;template:string;
  created_at:string|null;generation_model:string|null;
  facts_used:{fact_id:string;kind:string;text:string;source:string;confidence:number;verified:boolean;user_approved:boolean}[];
  missing_fact_ids:string[];
  generation_context?:{mode:string;job_title?:string;job_skills?:string[];matched_skills?:Record<string,string[]>;original_fact_ids?:string[];ordered_fact_ids?:string[];selection_mode?:string;selected_fact_ids?:string[];omitted_fact_ids?:string[];emphasized_fact_ids?:string[];contact_header?:string[]}|null;
};

export function DocumentHistory({documents}:{documents:GeneratedDocument[]}) {
  return <><ResumeComparison key={documents.map(row=>row.id).join('|')} documents={documents}/>{documents.map(document=><section className="document" key={document.id} aria-label={`${document.kind} version ${document.version}`}>
    <strong>{document.kind} · v{document.version}</strong>
    {document.generation_context?.mode==='APPROVED_SKILL_ORDER'&&(document.generation_context.selection_mode==='EXPLICIT_APPROVED_FACTS'
      ?<p>Tailored resume: selected facts for {document.generation_context.job_title}. Approved facts included: {document.generation_context.selected_fact_ids?.length}. Omitted from this resume: {document.generation_context.omitted_fact_ids?.length}. Approved wording preserved.</p>
      :<p>Tailored resume: skill order for {document.generation_context.job_title}. Wording and work history preserved.</p>)}
    <p>{document.fact_ids.length} factual sources · profile snapshot {document.profile_version.slice(0,10)}</p>
    {document.generation_context?.emphasized_fact_ids&&<p>Job-matched skill and technology facts in bold: {document.generation_context.emphasized_fact_ids.length}. DOCX and PDF preserve the same approved text as TXT.</p>}
    {(['pdf','docx','txt'] as const).filter(format=>document.files[format]).map(format=><a key={format} className="button-link" href={'/documents/'+encodeURIComponent(document.id)+'/'+format}>Download {format.toUpperCase()}</a>)}
    <details><summary>Generation details and facts</summary>
      <p>Generated: {document.created_at ? new Date(document.created_at).toLocaleString() : 'Not recorded for this historical document'}</p>
      <p>Generation method: {document.generation_model || 'Not recorded for this historical document'}</p>
      <p>Template: {document.template}</p>
      <p>Profile snapshot: <code>{document.profile_version}</code></p>
      {document.generation_context?.contact_header&&<section aria-label="Saved resume contact header" style={{overflowWrap:'anywhere'}}><h4>Saved contact details included</h4>{document.generation_context.contact_header.map((line,index)=><p key={index}>{line}</p>)}</section>}
      {document.generation_context?.mode==='APPROVED_SKILL_ORDER'&&<p>Target skill snapshot: {document.generation_context.job_skills?.join(', ')||'No skills detected'}. Matches guide ordering; they are not a qualification assessment.</p>}
      {document.missing_fact_ids.length>0&&<p className="alert">Some original facts are unavailable: {document.missing_fact_ids.join(', ')}</p>}
      <ul>{document.facts_used.map(fact=><li key={fact.fact_id}><strong>{fact.kind}</strong>: {fact.text}<br/>Source: {fact.source}<br/>
        At generation: {fact.verified?'verified':'unverified'}, {fact.user_approved?'approved':'not approved'}, confidence {fact.confidence}%
        {!!document.generation_context?.matched_skills?.[fact.fact_id]?.length&&<p>Matched job skills: {document.generation_context.matched_skills[fact.fact_id].join(', ')}</p>}
        {document.generation_context?.emphasized_fact_ids?.includes(fact.fact_id)&&<p>Emphasized in DOCX and PDF.</p>}</li>)}</ul>
      <p>Checksums (SHA-256)</p><ul>{Object.entries(document.sha256).map(([format,hash])=><li key={format}>{format.toUpperCase()}: <code style={{overflowWrap:'anywhere'}}>{hash}</code></li>)}</ul>
    </details>
  </section>)}</>;
}
