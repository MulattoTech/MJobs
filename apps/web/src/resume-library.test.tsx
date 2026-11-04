// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {api, uploadResume} from './api';
import {ResumeLibrary, type Resume} from './resume-library';

vi.mock('./api', () => ({api: vi.fn(), uploadResume: vi.fn()}));

const old: Resume = {
  id:'old', profile_id:'fixture', filename:'original.pdf', format:'pdf',
  size_bytes:100, sha256:'first-hash', status:'STORED',
  created_at:'2026-10-01T00:00:00Z', series_id:'engineering', label:'Engineering', version:1,
};
const current: Resume = {
  ...old, id:'current', filename:'latest.pdf', sha256:'second-hash',
  created_at:'2026-10-02T00:00:00Z', version:2,
};
const extraction = {
  status:'READY_FOR_REVIEW', warnings:[], contact:{}, facts:[],
};
function finished(id:string, when:string) {
  return {operation_id:'op-'+id, request_id:'req-'+id, state:'COMPLETED',
    current_step:null, progress:100, error:null, completed_at:when, updated_at:when,
    result:extraction};
}
function renderLibrary() {
  return <ResumeLibrary profileId="fixture" saved resumes={[old,current]} busy={false}
    action={async (_label,run)=>{await run();}} onStored={vi.fn()} onApply={vi.fn()}/>;
}
describe('base resume extraction review provenance', () => {
  beforeEach(()=>{localStorage.clear();vi.mocked(api).mockReset();vi.mocked(uploadResume).mockReset();});

  it('identifies the actual saved version selected by the durable latest-status readback',async()=>{
    vi.mocked(api).mockImplementation(async path=>{
      if(path.endsWith('/resumes/old/extract/queued')) return finished('old','2026-10-01T10:00:00Z') as never;
      if(path.endsWith('/resumes/current/extract/queued')) return finished('current','2026-10-02T10:00:00Z') as never;
      throw new Error('Unexpected synthetic path: '+path);
    });
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>{root.render(renderLibrary());await Promise.resolve();});
      expect(container.textContent).toContain('Review source: Engineering · version 2 · latest.pdf');
      expect(container.querySelector('[data-extraction-source]')?.getAttribute('data-extraction-source')).toBe('current');
      expect(container.textContent).not.toContain('Review source: Engineering · version 1 · original.pdf');
    } finally {await act(async()=>root.unmount());container.remove();}
  });

  it('rebinds the review to a deliberately extracted older version, not the previous latest',async()=>{
    vi.mocked(api).mockImplementation(async (path,init)=>{
      if(path.endsWith('/resumes/old/extract/queued')&&init?.method==='POST')
        return {operation_id:'op-old', request_id:'req-old', state:'QUEUED', progress:0} as never;
      if(path.endsWith('/resumes/old/extract/queued/op-old'))
        return finished('old','2026-10-03T10:00:00Z') as never;
      if(path.endsWith('/resumes/old/extract/queued'))
        return finished('old','2026-10-01T10:00:00Z') as never;
      if(path.endsWith('/resumes/current/extract/queued'))
        return finished('current','2026-10-02T10:00:00Z') as never;
      throw new Error('Unexpected synthetic path: '+path);
    });
    localStorage.setItem('mjobs.resume-extraction-intent.v1:fixture:old','req-old');
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>{root.render(renderLibrary());await Promise.resolve();});
      expect(container.textContent).toContain('Review source: Engineering · version 2 · latest.pdf');
      const older=container.querySelector('[data-resume-version="1"] button') as HTMLButtonElement;
      await act(async()=>{older.click();await Promise.resolve();});
      expect(container.textContent).toContain('Review source: Engineering · version 1 · original.pdf');
      expect(container.querySelector('[data-extraction-source]')?.getAttribute('data-extraction-source')).toBe('old');
      expect(container.textContent).not.toContain('Review source: Engineering · version 2 · latest.pdf');
    } finally {await act(async()=>root.unmount());container.remove();}
  });
});
