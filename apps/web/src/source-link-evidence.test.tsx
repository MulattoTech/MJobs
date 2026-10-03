import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourceLinkEvidence } from './source-link-evidence';

describe('job link observations', () => {
  it('shows not checked when no history exists', () => {
    const html = renderToStaticMarkup(<SourceLinkEvidence />);
    expect(html).toContain('Job link observations');
    expect(html).toContain('Not checked');
    expect(html).not.toContain('0%');
  });

  it('keeps an empty observation window as not checked', () => {
    const html = renderToStaticMarkup(<SourceLinkEvidence evidence={{
      sampled_count: 0, accepted_posting_count: 0, conclusive_sample_count: 0, accurate_sample_count: 0, unknown_sample_count: 0,
      scope: 'COMPANY_SOURCE', latest: [],
    }} />);
    expect(html).toContain('Not checked');
    expect(html).not.toContain('Observed 0');
  });

  it('shows bounded counts and detailed identifier outcomes', () => {
    const html = renderToStaticMarkup(<SourceLinkEvidence evidence={{
      sampled_count: 5, accepted_posting_count: 25, conclusive_sample_count: 1, accurate_sample_count: 1, unknown_sample_count: 4,
      scope: 'COMPANY_SOURCE', latest: [{posting_id:'posting-1', requested_url:'https://jobs.example/a',
        final_url:'https://jobs.example/a', result:'ACCURATE', reason:'Posting identifier matched',
        observed_at:'2026-10-03T12:00:00Z', response_sha256:null, identity_evidence:['same requisition']}],
    }} />);
    expect(html).toContain('5 sampled posting observations across 25 accepted feed rows in this window. Observed 5 bounded links: 1 identifier match, 0 not found, 4 unknown.');
    expect(html).toContain('Conclusive checks: 1/1 matched (100% among conclusive checks only).');
    expect(html).toContain('Posting identifier matched');
    expect(html).toContain('Requested: <code>https://jobs.example/a</code>');
    expect(html).toContain('do not verify an employer');
  });
});
