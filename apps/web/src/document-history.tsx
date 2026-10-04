import React from 'react';

export type GeneratedDocument = {
  id:string;kind:string;version:number;profile_version:string;fact_ids:string[];
  files:Record<string,string>;sha256:Record<string,string>;template:string;
  created_at:string|null;generation_model:string|null;
  facts_used:{fact_id:string;kind:string;text:string;source:string;confidence:number;verified:boolean;user_approved:boolean}[];
  missing_fact_ids:string[];
};

export function DocumentHistory({documents}:{documents:GeneratedDocument[]}) {
  return <>{documents.map(document=><section className="document" key={document.id} aria-label={`${document.kind} version ${document.version}`}>
    <strong>{document.kind} · v{document.version}</strong>
    <p>{document.fact_ids.length} factual sources · profile snapshot {document.profile_version.slice(0,10)}</p>
    {(['pdf','docx','txt'] as const).filter(format=>document.files[format]).map(format=><a key={format} className="button-link" href={'/documents/'+encodeURIComponent(document.id)+'/'+format}>Download {format.toUpperCase()}</a>)}
    <details><summary>Generation details and facts</summary>
      <p>Generated: {document.created_at ? new Date(document.created_at).toLocaleString() : 'Not recorded for this historical document'}</p>
      <p>Generation method: {document.generation_model || 'Not recorded for this historical document'}</p>
      <p>Template: {document.template}</p>
      <p>Profile snapshot: <code>{document.profile_version}</code></p>
      {document.missing_fact_ids.length>0&&<p className="alert">Some original facts are unavailable: {document.missing_fact_ids.join(', ')}</p>}
      <ul>{document.facts_used.map(fact=><li key={fact.fact_id}><strong>{fact.kind}</strong>: {fact.text}<br/>Source: {fact.source}<br/>
        At generation: {fact.verified?'verified':'unverified'}, {fact.user_approved?'approved':'not approved'}, confidence {fact.confidence}%</li>)}</ul>
      <p>Checksums (SHA-256)</p><ul>{Object.entries(document.sha256).map(([format,hash])=><li key={format}>{format.toUpperCase()}: <code style={{overflowWrap:'anywhere'}}>{hash}</code></li>)}</ul>
    </details>
  </section>)}</>;
}
