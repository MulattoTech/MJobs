import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  CoverLetterPreviewError,
  CoverLetterReviewPreview,
  CoverLetterReviewPreviewCard,
  createCoverLetterPreviewClient,
  mapApprovedCoverLetterFactSources,
  validateCoverLetterPreview,
  type CoverLetterPreviewResult,
} from './cover-letter-review-preview';

const selection={
  profileId:'profile-exact',profileVersion:8,jobId:'job-exact',jobFingerprint:'c'.repeat(64),
  jobTitle:'Platform Administrator',company:'Northwind Systems',
};
const facts=[
  {id:'approved-linux',text:'Maintained Linux infrastructure for production services.',source:'Verified employer record · 2025',verified:true,user_approved:true},
  {id:'pending-cert',text:'New certification claim.',source:'User draft',verified:false,user_approved:false},
];

function preview(overrides:Partial<CoverLetterPreviewResult>={}):CoverLetterPreviewResult {
  return {
    profile_id:'profile-exact',profile_version:8,job_id:'job-exact',job_fingerprint:'c'.repeat(64),
    preview_text:'Alex Example\nApplication for Platform Administrator at Northwind Systems\nMaintained Linux infrastructure.',
    fact_ids:['approved-linux'],matched_terms:{'approved-linux':['linux','infrastructure']},
    responsibilities:['Maintain and troubleshoot Linux systems'],
    skills:['Linux','Python'],skill_strengths:{Linux:'REQUIRED',Python:'PREFERRED'},
    company_identity:'SOURCE_LISTED',
    company_context:{content_origin:'USER_PROVIDED',content_status:'UNVERIFIED',
      content:'Northwind supports regional infrastructure.',source_url:'https://careers.northwind.example/about'},
    read_only:true,document_created:false,...overrides,
  };
}

describe('user-reviewed cover letter preview',()=>{
  it('requests a read-only preview for the exact selected scope and options',async()=>{
    const api=vi.fn(async()=>preview());
    const client=createCoverLetterPreviewClient({...selection,api});

    const result=await client.generate({style:'technical',length:'DETAILED'});

    expect(api).toHaveBeenCalledTimes(1);
    expect(api).toHaveBeenCalledWith(
      '/profiles/profile-exact/jobs/job-exact/cover-letter-preview',
      {method:'POST',headers:{'content-type':'application/json','accept':'application/json'},
        body:JSON.stringify({expected_profile_version:8,expected_job_fingerprint:'c'.repeat(64),style:'technical',length:'DETAILED'})},
    );
    expect(result.profile_id).toBe('profile-exact');
    expect(result.job_id).toBe('job-exact');
  });

  it('rejects stale profile/job scope and changed revision metadata',()=>{
    expect(()=>validateCoverLetterPreview(preview({profile_id:'another-profile'}),selection)).toThrow(CoverLetterPreviewError);
    expect(()=>validateCoverLetterPreview(preview({job_id:'another-job'}),selection)).toThrow(CoverLetterPreviewError);
    expect(()=>validateCoverLetterPreview(preview({profile_version:7}),selection)).toThrow(CoverLetterPreviewError);
    expect(()=>validateCoverLetterPreview(preview({job_fingerprint:'d'.repeat(64)}),selection)).toThrow(CoverLetterPreviewError);
  });

  it('rejects any response that does not prove preview-only behavior',()=>{
    expect(()=>validateCoverLetterPreview({...preview(),read_only:false},selection)).toThrow(CoverLetterPreviewError);
    expect(()=>validateCoverLetterPreview({...preview(),document_created:true},selection)).toThrow(CoverLetterPreviewError);
  });

  it('shows only returned facts with unique verified, approved source provenance',()=>{
    expect(mapApprovedCoverLetterFactSources(preview(),facts)).toEqual([
      {id:'approved-linux',text:'Maintained Linux infrastructure for production services.',
        source:'Verified employer record · 2025',matchedTerms:['linux','infrastructure']},
    ]);
    expect(()=>mapApprovedCoverLetterFactSources(preview({fact_ids:['pending-cert']}),facts)).toThrow(CoverLetterPreviewError);
    expect(()=>mapApprovedCoverLetterFactSources(preview({fact_ids:['missing']}),facts)).toThrow(CoverLetterPreviewError);
    expect(()=>mapApprovedCoverLetterFactSources(preview(),[facts[0],facts[0]])).toThrow(CoverLetterPreviewError);
  });

  it('renders the draft, exact fact source, job evidence/strength, and unverified company context',()=>{
    const html=renderToStaticMarkup(<CoverLetterReviewPreviewCard
      profileId={selection.profileId} jobId={selection.jobId} jobTitle={selection.jobTitle}
      company={selection.company} approvedFacts={facts} preview={preview()}/>);

    expect(html).toContain('Alex Example');
    expect(html).toContain('Approved fact sources');
    expect(html).toContain('Verified employer record · 2025');
    expect(html).toContain('Matched job terms: linux, infrastructure');
    expect(html).toContain('Source: saved job posting job-exact');
    expect(html).toContain('Maintain and troubleshoot Linux systems');
    expect(html).toContain('Required');
    expect(html).toContain('Preferred');
    expect(html).toContain('USER_PROVIDED / CONTENT UNVERIFIED');
    expect(html).toContain('Northwind supports regional infrastructure.');
    expect(html).toContain('No document is saved by preview');
    expect(html).not.toContain('Submit application');
  });

  it('labels absent company context without inventing content',()=>{
    const html=renderToStaticMarkup(<CoverLetterReviewPreviewCard
      profileId={selection.profileId} jobId={selection.jobId} jobTitle={selection.jobTitle}
      company={selection.company} approvedFacts={facts} preview={preview({company_context:null})}/>);

    expect(html).toContain('No reviewed company context was included in this preview.');
    expect(html).not.toContain('CONTENT UNVERIFIED');
  });

  it('waits for an explicit preview action and names the exact current selection',()=>{
    const api=vi.fn();
    const html=renderToStaticMarkup(<CoverLetterReviewPreview
      {...selection} approvedFacts={facts} api={api}/>);

    expect(html).toContain('aria-label="Cover letter review preview"');
    expect(html).toContain('profile-exact');
    expect(html).toContain('job-exact');
    expect(html).toContain('Preview cover letter');
    expect(html).toContain('No document is saved until you choose the existing Prepare flow');
    expect(html).not.toContain('Application for Platform Administrator at Northwind Systems');
    expect(api).not.toHaveBeenCalled();
  });

  it('turns stale API conflicts into a clear safe-to-recover message',async()=>{
    const api=vi.fn().mockRejectedValue({status:409,code:'TARGET_JOB_CHANGED',message:'private server detail'});
    const client=createCoverLetterPreviewClient({...selection,api});

    await expect(client.generate({style:'professional',length:'NORMAL'})).rejects.toMatchObject({
      code:'STALE_SELECTION',message:expect.stringContaining('Reload the selected profile and job'),
    });
    await expect(client.generate({style:'professional',length:'NORMAL'})).rejects.not.toThrow('private server detail');
  });
});
