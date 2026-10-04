import React, {useEffect,useRef,useState} from 'react';
import {api,resumeMediaType} from './api';
import {DocumentHistory,type GeneratedDocument} from './document-history';
import type {Resume} from './resume-library';

export const documentKinds:Record<string,string>={BASE_RESUME:'Base resume',MASTER_RESUME:'Master resume',TAILORED_RESUME:'Tailored resume',COVER_LETTER:'Cover letter',APPLICATION_ANSWER_EXPORT:'Application answer export',SUPPORTING_DOCUMENT:'Supporting document',CERTIFICATION:'Certification'};
type StoredDocument=Resume&{revision_id:string;document_kind:string;version_created_at:string};
type Library={uploaded:StoredDocument[];generated:(GeneratedDocument&{application_id:string;job_title:string;company:string})[]};

export function DocumentLibrary({profileId,saved,baseResumes,busy,action,onBaseStored}:{
  profileId:string;saved:boolean;baseResumes:Resume[];busy:boolean;
  action:(label:string,run:()=>Promise<void>)=>Promise<void>;onBaseStored:()=>Promise<void>;
}) {
  const [library,setLibrary]=useState<Library>({uploaded:[],generated:[]});
  const [kind,setKind]=useState('SUPPORTING_DOCUMENT');
  const [series,setSeries]=useState('');const [label,setLabel]=useState('');
  const [file,setFile]=useState<File|null>(null);const [error,setError]=useState('');
  const input=useRef<HTMLInputElement>(null);const sequence=useRef(0);const mounted=useRef(true);
  const path='/profiles/'+encodeURIComponent(profileId)+'/document-library';
  async function refresh() {
    const current=++sequence.current;
    const result=await api<Library>(path);
    if(mounted.current&&current===sequence.current){setLibrary(result);setError('');}
  }
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;++sequence.current;};},[]);
  useEffect(()=>{if(saved)void refresh().catch(e=>{if(mounted.current)setError(String(e));});else setLibrary({uploaded:[],generated:[]});},[profileId,saved,baseResumes]);
  async function store() {
    if(!file)return;
    if(!file.size||file.size>10*1024*1024)throw new Error('Document must be nonempty and no larger than 10 MiB.');
    await api(path,{method:'POST',body:file,headers:{'Content-Type':resumeMediaType(file.name),'X-Filename':encodeURIComponent(file.name),
      'X-Document-Kind':kind,...(series?{'X-Document-Series':series}:{'X-Document-Label':encodeURIComponent(label)})}});
    if(!mounted.current)return;
    await refresh();
    if(!mounted.current)return;
    if(kind==='BASE_RESUME')await onBaseStored();
    if(!mounted.current)return;
    setFile(null);setLabel('');if(input.current)input.current.value='';
  }
  const groups=Array.from(new Map(library.uploaded.map(row=>[row.series_id,row])).values());
  return <section className="panel" aria-label="Document library"><h2>Document library</h2>
    <p>Keep original files and their versions together. Document types describe your files; uploading never verifies career facts or approves a document for submission.</p>
    {!saved?<p>Save your profile before adding documents.</p>:<>
      <div className="two-col"><label>Document type<select aria-label="Document type" value={kind} disabled={busy} onChange={e=>{setKind(e.target.value);setSeries('');}}>{Object.entries(documentKinds).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label>
      <label>Document collection<select aria-label="Document collection" value={series} disabled={busy} onChange={e=>setSeries(e.target.value)}><option value="">New collection</option>{groups.filter(row=>row.document_kind===kind).map(row=><option key={row.series_id} value={row.series_id}>{row.label}</option>)}</select></label></div>
      {!series&&<label>Document label<input maxLength={200} value={label} onChange={e=>setLabel(e.target.value)}/></label>}
      <label>Choose a document (PDF, DOCX or TXT; up to 10 MiB)<input ref={input} type="file" accept=".pdf,.docx,.txt" disabled={busy} onChange={e=>setFile(e.target.files?.[0]||null)}/></label>
      <button type="button" disabled={busy||!file} onClick={()=>void action('Storing document',store)}>Store document</button>{' '}
      <button type="button" disabled={busy} onClick={()=>void action('Refreshing document library',refresh)}>Refresh library</button>
      {error&&<p role="alert">{error}</p>}
      {groups.map(group=><section key={group.series_id} aria-label={group.label+' collection'}><h3>{group.label} · {documentKinds[group.document_kind]}</h3>
        {library.uploaded.filter(row=>row.series_id===group.series_id).sort((a,b)=>b.version-a.version).map(row=><div className="document" key={row.revision_id}>
          <strong>Version {row.version} · {row.filename}</strong><p>Stored unreviewed · {row.format.toUpperCase()} · {new Date(row.version_created_at).toLocaleString()}</p>
          <a className="button-link" href={path+'/'+encodeURIComponent(row.revision_id)}>Download original version {row.version}</a>
          <details><summary>File checksum</summary><code style={{overflowWrap:'anywhere'}}>SHA-256: {row.sha256}</code></details>
        </div>)}
      </section>)}
      <h3>Generated application documents</h3><p>These retain their application revision and original profile snapshot. Generic generated resumes are not labeled as tailored.</p>
      {Array.from(new Map(library.generated.map(row=>[row.application_id,row])).values()).map(application=><section key={application.application_id}>
        <h4>{application.job_title} · {application.company}</h4>
        <DocumentHistory documents={library.generated.filter(row=>row.application_id===application.application_id)}/>
      </section>)}
      {!library.uploaded.length&&!library.generated.length&&<p>No documents stored for this profile.</p>}
    </>}
  </section>;
}
