import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationPriority, type ApplicationPriorityExplanation } from './application-priority';
import { ApplicationPriorityPanel, loadApplicationPriority, mapPriorityPanelState, type ApplicationPriorityApiResponse } from './application-priority-panel';

const response:ApplicationPriorityExplanation & {unknowns:string[]} = {
  profile_id:'profile-a',job_id:'job-a',priority_score:73,match_score:91,priority_band:'HIGH',coverage_percent:75,
  advisory:true,affects_match_score:false,unknowns:['company_preference'],
  factors:[
    {key:'posting_age',label:'Posting age',score:100,contribution:16,status:'KNOWN',explanation:'Recently posted.',source:'job posting'},
    {key:'company_preference',label:'Company preference',score:null,contribution:null,status:'UNKNOWN',explanation:'No preference is set.',source:'profile settings'},
  ],
};

function findButton(node:React.ReactNode):React.ReactElement<{type?:string;onClick?:()=>void;children?:React.ReactNode}>|null {
  if(Array.isArray(node)) {
    for(const child of node) {
      const match=findButton(child);
      if(match) return match;
    }
    return null;
  }
  if(!React.isValidElement(node)) return null;
  const element=node as React.ReactElement<{type?:string;onClick?:()=>void;children?:React.ReactNode}>;
  if(element.type==='button') return element;
  return findButton(element.props.children);
}

describe('ApplicationPriorityPanel',()=>{
  it('requests only the selected profile/job and composes a separate advisory explanation',async()=>{
    const getApplicationPriority=vi.fn(async(profileId:string,jobId:string)=>({...response,profile_id:profileId,job_id:jobId}));
    const loaded=await loadApplicationPriority(getApplicationPriority,'profile-a','job-a');
    expect(getApplicationPriority).toHaveBeenCalledExactlyOnceWith('profile-a','job-a');
    expect(loaded.state).toBe('ready');
    const mapped=mapPriorityPanelState('profile-a','job-a',loaded);
    const html=renderToStaticMarkup(<ApplicationPriority {...mapped} onRetry={vi.fn()}/>);
    expect(html).toContain('Application priority is advisory');
    expect(html).toContain('73/100');
    expect(html).toContain('91/100');
    expect(html).toContain('Posting age');
    expect(html).toContain('Recently posted.');
    expect(html).toContain('Company preference');
    expect(html).toContain('UNKNOWN');
  });

  it('hides a result returned for another profile or job',async()=>{
    for(const returned of [
      {...response,profile_id:'profile-other'},
      {...response,job_id:'job-other'},
    ]) {
      const loaded=await loadApplicationPriority(vi.fn(async()=>returned),'profile-a','job-a');
      const mapped=mapPriorityPanelState('profile-a','job-a',loaded);
      const html=renderToStaticMarkup(<ApplicationPriority {...mapped} onRetry={vi.fn()}/>);
      expect(html).toContain('does not match the selected profile and job');
      expect(html).not.toContain('Recently posted.');
      expect(html).not.toContain('73/100');
    }
  });

  it('rejects malformed scores, factors, and unknown lists into a retryable failure',async()=>{
    const malformed=[
      {...response,priority_score:NaN},
      {...response,factors:[null]},
      {...response,unknowns:[]},
    ];
    for(const value of malformed) {
      const loaded=await loadApplicationPriority(vi.fn(async()=>value as unknown as ApplicationPriorityApiResponse),'profile-a','job-a');
      expect(loaded.state).toBe('error');
      const mapped=mapPriorityPanelState('profile-a','job-a',loaded);
      const html=renderToStaticMarkup(<ApplicationPriority {...mapped} onRetry={vi.fn()}/>);
      expect(html).toContain('role="alert"');
      expect(html).toContain('Retry');
      expect(html).not.toContain('73/100');
    }
  });

  it('never renders score data when the API does not mark it advisory-only',async()=>{
    const malformed={...response,advisory:false} as unknown as ApplicationPriorityApiResponse;
    const loaded=await loadApplicationPriority(vi.fn(async()=>malformed),'profile-a','job-a');
    expect(loaded.state).toBe('error');
    const mapped=mapPriorityPanelState('profile-a','job-a',loaded);
    const html=renderToStaticMarkup(<ApplicationPriority {...mapped} onRetry={vi.fn()}/>);
    expect(html).toContain('Application priority could not be loaded');
    expect(html).toContain('Retry');
    expect(html).not.toContain('73/100');
    expect(html).not.toContain('Recently posted.');
  });

  it('fails safely when the injected client rejects and does not resubmit automatically',async()=>{
    const getApplicationPriority=vi.fn(async()=>{throw new Error('private transport details');});
    const loaded=await loadApplicationPriority(getApplicationPriority,'profile-a','job-a');
    expect(loaded.state).toBe('error');
    const onRetry=vi.fn();
    const mapped=mapPriorityPanelState('profile-a','job-a',loaded);
    const html=renderToStaticMarkup(<ApplicationPriority {...mapped} onRetry={onRetry}/>);
    expect(html).toContain('Application priority could not be loaded');
    expect(html).not.toContain('private transport details');
    expect(html).toContain('<button type="button">Retry</button>');
    expect(html).not.toContain('tabindex="-1"');
    const retryButton=findButton(ApplicationPriority({...mapped,onRetry}));
    expect(retryButton?.props.type).toBe('button');
    retryButton?.props.onClick?.();
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(html).not.toMatch(/\b(Reject|Exclude|Submit|Apply now)\b/);
  });

  it('shows loading status and uses the injected read-only client on render',()=>{
    const getApplicationPriority=vi.fn(async()=>response);
    const html=renderToStaticMarkup(<ApplicationPriorityPanel profileId="profile-a" jobId="job-a" getApplicationPriority={getApplicationPriority}/>);
    expect(html).toContain('role="status"');
    expect(html).toContain('Loading application priority');
    expect(html).not.toContain('73/100');
    expect(getApplicationPriority).not.toHaveBeenCalled();
  });
});
