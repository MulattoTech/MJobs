import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ResumePageFit, canGeneratePageFit, type ResumePageFitPreview } from './resume-page-fit';

const preview:ResumePageFitPreview={
  preview_id:'preview-1',profile_id:'profile-a',job_id:'job-a',
  actual_page_count:2,unmet_target:true,measurement_status:'AVAILABLE',
  preview_pdf_url:'/profiles/profile-a/jobs/job-a/resume-page-fit-previews/preview-1/resume.pdf',
  generation_context:{page_fit:{page_target:1,page_count:2,attempts:3,measurement_available:true,target_met:false,reason:null,omitted_facts:[{fact_id:'fact-omitted',reason:'LOWER_RELEVANCE'}]}},
};

describe('resume PDF page fit review',()=>{
  it('shows the actual measured PDF page count and clearly reports an unmet target',()=>{
    const html=renderToStaticMarkup(<ResumePageFit profileId="profile-a" jobId="job-a" preview={preview} onPreview={vi.fn()} onGenerate={vi.fn()}/>);
    expect(html).toContain('Measured PDF pages: 2');
    expect(html).toContain('Preview for target: 1 page');
    expect(html).toContain('The measured PDF does not meet the requested page target.');
    expect(html).toContain('LOWER_RELEVANCE');
    expect(html).toContain('Open measured PDF preview');
    expect(html).toContain('resume-page-fit-previews/preview-1/resume.pdf');
    expect(html).not.toContain('DOCX pages');
  });

  it('reports unavailable measurement without guessing and keeps generation disabled',()=>{
    const html=renderToStaticMarkup(<ResumePageFit profileId="profile-a" jobId="job-a" preview={{...preview,actual_page_count:null,measurement_status:'UNAVAILABLE',unmet_target:false}} onPreview={vi.fn()} onGenerate={vi.fn()}/>);
    expect(html).toContain('PDF page count unavailable');
    expect(html).toContain('The page target could not be measured; no page count is estimated.');
    expect(html).not.toContain('Measured PDF pages: 0');
    expect(html).toContain('disabled=""');
  });

  it('only enables generation for a reviewed, measured preview scoped to the selected profile and resume',()=>{
    expect(canGeneratePageFit(preview,'profile-a','job-a',1,true)).toBe(true);
    expect(canGeneratePageFit(preview,'profile-a','job-a',1,false)).toBe(false);
    expect(canGeneratePageFit({...preview,actual_page_count:null,measurement_status:'UNAVAILABLE'},'profile-a','job-a',1,true)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-b','job-a',1,true)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-a','job-b',1,true)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-a','job-a',2,true)).toBe(false);
  });
});
