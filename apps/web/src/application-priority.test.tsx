import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationPriority, type ApplicationPriorityExplanation } from './application-priority';

const explanation:ApplicationPriorityExplanation = {
  profile_id:'profile-a',job_id:'job-a',priority_score:78,match_score:94,priority_band:'HIGH',coverage_percent:75,
  advisory:true,affects_match_score:false,
  factors:[
    {key:'freshness',label:'Posting freshness',score:100,contribution:18,status:'KNOWN',explanation:'Posted recently.',source:'job posting'},
    {key:'company_preference',label:'Company preference',score:null,contribution:null,status:'UNKNOWN',explanation:'No company preference is configured.',source:'profile settings'},
  ],
};

const render = (overrides:Partial<Parameters<typeof ApplicationPriority>[0]> = {}) => renderToStaticMarkup(
  <ApplicationPriority profileId="profile-a" jobId="job-a" state="ready" explanation={explanation} onRetry={vi.fn()} {...overrides} />,
);

describe('ApplicationPriority',()=>{
  it('separates advisory priority from match score and shows factor evidence',()=>{
    const html=render();
    expect(html).toContain('Application priority is advisory');
    expect(html).toContain('78');
    expect(html).toContain('HIGH');
    expect(html).toContain('Match score');
    expect(html).toContain('94');
    expect(html).toContain('does not change match score or job eligibility');
    expect(html).toContain('Posting freshness');
    expect(html).toContain('Posted recently.');
    expect(html).toContain('job posting');
    expect(html).toContain('Company preference');
    expect(html).toContain('UNKNOWN');
    expect(html).toContain('No company preference is configured.');
    expect(html).toContain('75%');
  });

  it('fails closed when the returned profile or job scope is stale',()=>{
    const foreignProfile=render({explanation:{...explanation,profile_id:'profile-b'}});
    const foreignJob=render({explanation:{...explanation,job_id:'job-b'}});
    expect(foreignProfile).toContain('does not match the selected profile and job');
    expect(foreignProfile).not.toContain('Posted recently.');
    expect(foreignJob).toContain('does not match the selected profile and job');
    expect(foreignJob).not.toContain('Posted recently.');
  });

  it('shows a loading state without presenting stale scores',()=>{
    const html=render({state:'loading'});
    expect(html).toContain('Loading application priority');
    expect(html).not.toContain('78');
  });

  it('shows an accessible error and retry action without priority data',()=>{
    const onRetry=vi.fn();
    const html=render({state:'error',explanation:null,onRetry});
    expect(html).toContain('role="alert"');
    expect(html).toContain('Application priority could not be loaded');
    expect(html).toContain('Retry');
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('handles empty and malformed responses without inventing a score or adding controls',()=>{
    const empty=render({state:'ready',explanation:null});
    expect(empty).toContain('No application priority explanation is available');
    expect(empty).not.toContain('0/100');
    const invalid=render({explanation:{...explanation,advisory:false} as unknown as ApplicationPriorityExplanation});
    expect(invalid).toContain('could not be verified as advisory');
    expect(invalid).not.toContain('Posted recently.');
    const html=render();
    expect(html).not.toMatch(/\b(Reject|Exclude|Submit|Apply now)\b/);
  });
});
