import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { FollowUpDraftsView, followUpDraftsPath, clipboardErrorAfterContextChange, type FollowUpDraftCandidate } from './application-follow-up-drafts';

const candidate:FollowUpDraftCandidate={
  application_id:'app-1',job_id:'job-1',job_title:'Systems Administrator',company:'Acme',days_quiet:8,
  reported_at:'2026-10-01T12:00:00Z',attribution:'EXTERNAL_USER_REPORTED',verified:false,
  email_subject:'Following up on my application',email_body:'I applied and would appreciate an update.',
  dm_body:'Hello, I recently applied and wanted to follow up.',
  character_limits:{subject:120,email_body:500,dm_body:280},
};

describe('follow-up drafts UI',()=>{
  it('clears clipboard errors when profile or quiet-days context changes',()=>{
    const error={context:JSON.stringify(['profile-a',7]),message:'Clipboard access is unavailable.'};
    expect(clipboardErrorAfterContextChange(error,JSON.stringify(['profile-a',7]),JSON.stringify(['profile-a',7]))).toEqual(error);
    expect(clipboardErrorAfterContextChange(error,JSON.stringify(['profile-a',7]),JSON.stringify(['profile-a',14]))).toBeNull();
    expect(clipboardErrorAfterContextChange(error,JSON.stringify(['profile-a',7]),JSON.stringify(['profile-b',7]))).toBeNull();
  });

  it('builds the profile-scoped local API request with the selected quiet-days threshold',()=>{
    expect(followUpDraftsPath('profile / one',7)).toBe('/profiles/profile%20%2F%20one/application-follow-up-drafts?quiet_days=7');
  });

  it('renders the bounded quiet-days selector with its accessible default',()=>{
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={true} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('Follow-up drafts');
    expect(html).toContain('Quiet days before follow-up');
    expect(html).toContain('value="1">1 day</option>');
    expect(html).toContain('value="90">90 days</option>');
    expect(html).toContain('<option value="7" selected="">7 days</option>');
    expect(html).toContain('Loading follow-up drafts');
  });

  it('labels only reported and unverified evidence, shows bounded draft text, and offers copy only',()=>{
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[candidate]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('Acme');
    expect(html).toContain('Systems Administrator');
    expect(html).toContain('Reported by you · not verified');
    expect(html).toContain('Email subject');
    expect(html).toContain('Following up on my application');
    expect(html).toContain('30 / 120 characters');
    expect(html).toContain('I applied and would appreciate an update.');
    expect(html).toContain('DM wording');
    expect(html).toContain('Copy email subject');
    expect(html).toContain('Copy email body');
    expect(html).toContain('Copy DM wording');
    expect(html).not.toMatch(/>\s*Send\s*</i);
  });

  it('counts supplementary Unicode characters as one character against the API limit',()=>{
    const nearLimit={...candidate,email_subject:`${'a'.repeat(119)}😀`};
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[nearLimit]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('120 / 120 characters');
  });

  it('has explicit empty and API error states',()=>{
    const empty=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    const verified=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[{...candidate,verified:true}]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    const error=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={false} error="Local API unavailable" copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(empty).toContain('No reported applications have been quiet for 7 days or more.');
    expect(verified).toContain('No reported applications have been quiet for 7 days or more.');
    expect(verified).not.toContain('Following up on my application');
    expect(error).toContain('Could not load follow-up drafts');
    expect(error).toContain('Local API unavailable');
  });
});
