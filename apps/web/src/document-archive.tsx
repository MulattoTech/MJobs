import React from 'react';

export type ArchivableDocument = {
  id:string;
  profileId:string;
  applicationId?:string|null;
  revisionId?:string|null;
  label:string;
  kind:'resume'|'cover letter'|string;
  archivedAt:string|null;
};

function archivedDate(value:string) {
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString();
}

export function DocumentArchive({documents,busy=false,onArchive,onRestore}:{
  documents:ArchivableDocument[];
  busy?:boolean;
  onArchive:(document:ArchivableDocument)=>void;
  onRestore:(document:ArchivableDocument)=>void;
}) {
  return <section className="panel document-archive" aria-label="Document archive">
    <h2>Document archive</h2>
    <p>Archive keeps a document revision in your history while removing it from active documents. Review the document and its scope before choosing an action.</p>
    {!documents.length?<p>No resumes or cover letters are available to archive.</p>:<ul aria-label="Resumes and cover letters">
      {documents.map(document=>{
        const scope=[document.applicationId&&`application ${document.applicationId}`,document.revisionId&&`revision ${document.revisionId}`].filter(Boolean).join(' · ');
        const archived=document.archivedAt!==null;
        const actionName=`${archived?'Restore':'Archive'} ${document.kind} ${document.label}`;
        return <li key={`${document.profileId}:${document.applicationId||''}:${document.revisionId||document.id}`}>
          <article aria-label={`${document.kind}: ${document.label}`}>
            <h3>{document.label}</h3>
            <p>{document.kind}{scope?` · ${scope}`:''}</p>
            <p><strong>{archived?'Archived':'Active'}</strong>{archived?` · archived ${archivedDate(document.archivedAt!)}`:' · available in active documents'}</p>
            <button type="button" disabled={busy} aria-label={actionName+` for profile ${document.profileId}`}
              onClick={()=>archived?onRestore(document):onArchive(document)}>
              {archived?'Restore after review':'Archive after review'}
            </button>
          </article>
        </li>;
      })}
    </ul>}
  </section>;
}
