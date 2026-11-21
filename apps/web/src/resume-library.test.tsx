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
function renderLibrary(overrides:Partial<React.ComponentProps<typeof ResumeLibrary>>={}) {
  return <ResumeLibrary profileId="fixture" saved resumes={[old,current]} busy={false}
    action={async (_label,run)=>{await run();}} onStored={vi.fn()} onApply={vi.fn()} {...overrides}/>;
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

  it('withholds review and apply when saved metadata belongs to another profile',async()=>{
    vi.mocked(api).mockResolvedValue(finished('old','2026-10-01T10:00:00Z'));
    const onApply=vi.fn();
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>{
        root.render(renderLibrary({resumes:[{...old,profile_id:'other'}],onApply}));
      });
      expect(container.querySelector('[aria-label="Resume extraction review"]')).toBeNull();
      expect(container.querySelector('[role="alert"]')?.textContent).toContain('source resume version has changed or is unavailable');
      expect(container.textContent).not.toContain('Add selected to profile editor');
      expect(onApply).not.toHaveBeenCalled();
    } finally {await act(async()=>root.unmount());container.remove();}
  });

  it.each(['removed','replaced','profile-switched'] as const)(
    'clears selected suggestions when the source is %s',async change=>{
      vi.mocked(api).mockResolvedValue({
        ...finished('old','2026-10-01T10:00:00Z'),
        result:{...extraction,contact:{name:'Synthetic source name'}},
      });
      const onApply=vi.fn();
      const container=document.createElement('div');document.body.append(container);
      const root=createRoot(container);
      try {
        await act(async()=>{root.render(renderLibrary({resumes:[old],onApply}));});
        const checkbox=container.querySelector('.fact-suggestion input') as HTMLInputElement;
        expect(checkbox).not.toBeNull();
        await act(async()=>checkbox.click());
        expect(checkbox.checked).toBe(true);
        vi.mocked(api).mockRejectedValue(new Error('No extraction for changed scope'));
        const replacement=change==='removed'?[]:
          [{...old,...(change==='replaced'?{sha256:'replacement-hash'}:{profile_id:'other'})}];
        await act(async()=>{root.render(renderLibrary({
          profileId:change==='profile-switched'?'other':'fixture',resumes:replacement,onApply,
        }));});
        expect(container.querySelector('[aria-label="Resume extraction review"]')).toBeNull();
        expect(container.textContent).not.toContain('Synthetic source name');
        expect(container.textContent).not.toContain('Add selected to profile editor');
        expect(onApply).not.toHaveBeenCalled();
      } finally {await act(async()=>root.unmount());container.remove();}
    },
  );

  it('ignores a late extraction response after switching profiles',async()=>{
    let completeOld!:(value:ReturnType<typeof finished>)=>void;
    const pending=new Promise<ReturnType<typeof finished>>(resolve=>{completeOld=resolve;});
    vi.mocked(api).mockImplementation(async(path,init)=>{
      if(path==='/profiles/fixture/resumes/old/extract/queued'&&init?.method==='POST')
        return {operation_id:'op-old',state:'QUEUED',progress:0} as never;
      if(path==='/profiles/fixture/resumes/old/extract/queued/op-old')
        return await pending as never;
      if(path==='/profiles/other/resumes/current/extract/queued')
        return finished('current','2026-10-02T10:00:00Z') as never;
      throw new Error('No restored extraction for this scope');
    });
    localStorage.setItem('mjobs.resume-extraction-intent.v1:fixture:old','req-old');
    const onApply=vi.fn();
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>{root.render(renderLibrary({resumes:[old],onApply}));});
      const extract=container.querySelector('[data-resume-version="1"] button') as HTMLButtonElement;
      await act(async()=>extract.click());
      expect(vi.mocked(api)).toHaveBeenCalledWith('/profiles/fixture/resumes/old/extract/queued/op-old');
      await act(async()=>{root.render(renderLibrary({
        profileId:'other',resumes:[{...current,profile_id:'other',label:'Other profile'}],onApply,
      }));});
      expect(container.textContent).toContain('Review source: Other profile · version 2 · latest.pdf');
      await act(async()=>{completeOld(finished('old','2026-10-03T10:00:00Z'));});
      expect(container.textContent).toContain('Review source: Other profile · version 2 · latest.pdf');
      expect(container.textContent).not.toContain('Review source: Engineering · version 1 · original.pdf');
      expect(onApply).not.toHaveBeenCalled();
    } finally {await act(async()=>root.unmount());container.remove();}
  });

  it('applies only explicitly selected suggestions from a valid source and closes review',async()=>{
    const result={...extraction,contact:{name:'Synthetic source name'},
      facts:[{id:'skill-one',kind:'skill',text:'Synthetic skill',confidence:85}]};
    vi.mocked(api).mockResolvedValue({...finished('old','2026-10-01T10:00:00Z'),result});
    const onApply=vi.fn();
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>{root.render(renderLibrary({resumes:[old],onApply}));});
      const checkboxes=container.querySelectorAll<HTMLInputElement>('.fact-suggestion input');
      expect(checkboxes).toHaveLength(2);
      await act(async()=>checkboxes[1].click());
      const apply=Array.from(container.querySelectorAll('button'))
        .find(button=>button.textContent==='Add selected to profile editor')!;
      await act(async()=>apply.click());
      expect(onApply).toHaveBeenCalledExactlyOnceWith(result,new Set(['skill-one']),new Set());
      expect(container.querySelector('[aria-label="Resume extraction review"]')).toBeNull();
      expect(container.querySelector('[data-extraction-source]')).toBeNull();
    } finally {await act(async()=>root.unmount());container.remove();}
  });
});
