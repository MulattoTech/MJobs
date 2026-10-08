import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ResumePageFit, canGeneratePageFit, type ResumePageFitPreview } from './resume-page-fit';

const preview:ResumePageFitPreview={
  preview_id:'preview-1',profile_id:'profile-a',job_id:'job-a',
  actual_page_count:2,unmet_target:true,measurement_status:'AVAILABLE',
  preview_pdf_url:'/profiles/profile-a/jobs/job-a/resume-page-fit-previews/preview-1/resume.pdf',
  generation_context:{page_fit:{
    page_target:1,page_count:2,attempts:3,measurement_available:true,target_met:false,
    reason:null,omitted_facts:[{fact_id:'fact-omitted',reason:'PAGE_TARGET_FIT_LOWEST_RETENTION_RANK'}],
  }},
};

describe('resume PDF page fit job scope',()=>{
  it('opens the measured PDF preview before asking for explicit review',()=>{
    const html=renderToStaticMarkup(<ResumePageFit profileId="profile-a" jobId="job-a" preview={preview} onPreview={vi.fn()} onGenerate={vi.fn()}/>);
    const previewLink=html.indexOf('Open measured PDF preview');
    const reviewCheckbox=html.indexOf('I reviewed the measured PDF page-fit preview and omissions');
    expect(previewLink).toBeGreaterThanOrEqual(0);
    expect(html).toContain('href="/profiles/profile-a/jobs/job-a/resume-page-fit-previews/preview-1/resume.pdf"');
    expect(reviewCheckbox).toBeGreaterThan(previewLink);
    expect(html).toContain('max="20"');
  });

  it('does not guess a page count or permit generation when measurement is unavailable',()=>{
    const unavailable:ResumePageFitPreview={
      ...preview,actual_page_count:null,measurement_status:'UNAVAILABLE',unmet_target:false,
      generation_context:{page_fit:{...preview.generation_context.page_fit,page_count:null,measurement_available:false}},
    };
    const html=renderToStaticMarkup(<ResumePageFit profileId="profile-a" jobId="job-a" preview={unavailable} onPreview={vi.fn()} onGenerate={vi.fn()}/>);
    expect(html).toContain('PDF page count unavailable');
    expect(html).toContain('The page target could not be measured; no page count is estimated.');
    expect(html).toContain('disabled=""');
    expect(html).not.toContain('Measured PDF pages: 0');
    expect(canGeneratePageFit(unavailable,'profile-a','job-a',1,true)).toBe(false);
  });

  it('requires a reviewed measured preview for the same profile, job, and page target',()=>{
    expect(canGeneratePageFit(preview,'profile-a','job-a',1,true)).toBe(true);
    expect(canGeneratePageFit(preview,'profile-a','job-a',1,false)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-a','job-b',1,true)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-b','job-a',1,true)).toBe(false);
    expect(canGeneratePageFit(preview,'profile-a','job-a',2,true)).toBe(false);
    const twentyPagePreview:ResumePageFitPreview={
      ...preview,actual_page_count:20,unmet_target:false,
      generation_context:{page_fit:{...preview.generation_context.page_fit,page_target:20,page_count:20,target_met:true}},
    };
    expect(canGeneratePageFit(twentyPagePreview,'profile-a','job-a',20,true)).toBe(true);
    expect(canGeneratePageFit(twentyPagePreview,'profile-a','job-a',21,true)).toBe(false);
  });
});
