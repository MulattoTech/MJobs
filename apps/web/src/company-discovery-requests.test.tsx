import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { CompanyDiscoveryRequests, identityConfirmationMatches, type DiscoveryRequest, type IdentityConfirmationDraft } from './company-discovery-requests';

const row: DiscoveryRequest = {id:'request-1', revision:2, company_name:'Example Systems', job_title:'Python Engineer',
  reasons:['PREVIOUSLY_UNSEEN_COMPANY','HIGH_MATCH_JOB'], highest_match_score:91, status:'NEEDS_ORIGIN_REVIEW',
  reason:null, selected_website:null, authorization:null, operation_id:null, last_seen_at:'2026-10-03T12:00:00Z'};

describe('company discovery requests', () => {
  it('renders plain reasons, review safety copy, and the required website form', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryRequests rows={[row]} busy={false} onReview={vi.fn()} onDismiss={vi.fn()} />);
    expect(html).toContain('Company discovery requests');
    expect(html).toContain('Company not seen before');
    expect(html).toContain('Strong job match');
    expect(html).toContain('Website to review (HTTPS)');
    expect(html).toContain('<form>');
    expect(html).toContain('type="url" required="" pattern="https://.*"');
    expect(html).toContain('type="submit"');
    expect(html).toContain('Review website and discover');
    expect(html).toContain('Dismiss request');
    expect(html).toContain('Reviewing a site does not verify employer ownership');
    expect(html).not.toContain('PREVIOUSLY_UNSEEN_COMPANY');
  });

  it('leaves company identity confirmation unchecked and explains its limited meaning', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryRequests rows={[row]} busy={false} onReview={vi.fn()} onDismiss={vi.fn()} />);
    expect(html).toContain('I confirm this website belongs to Example Systems');
    expect(html).toContain('exact website above');
    expect(html).toContain('user-confirmed identity');
    expect(html).toContain('independent verification');
    expect(html).toContain('No application will be submitted');
    expect(html).not.toContain('type="checkbox" checked');
  });

  it('accepts identity confirmation only for the same request, revision, and exact website', () => {
    const draft: IdentityConfirmationDraft = {
      requestId: 'request-1', revision: 2, sourceWebsite: '', website: 'https://employer.example', confirmed: true,
    };
    expect(identityConfirmationMatches(row, 'https://employer.example', draft)).toBe(true);
    expect(identityConfirmationMatches(row, 'https://other.example', draft)).toBe(false);
    expect(identityConfirmationMatches({...row, revision: 3}, 'https://employer.example', draft)).toBe(false);
    expect(identityConfirmationMatches({...row, id: 'request-2'}, 'https://employer.example', draft)).toBe(false);
    expect(identityConfirmationMatches({...row, selected_website: 'https://approved.example'}, 'https://employer.example', draft)).toBe(false);
    expect(identityConfirmationMatches(row, 'https://employer.example', undefined)).toBe(false);
  });
  it('retains a selected website value and makes completed rows non-actionable', () => {
    const failed = renderToStaticMarkup(<CompanyDiscoveryRequests rows={[{...row, status:'FAILED', selected_website:'https://employer.example'}]} busy={false} onReview={vi.fn()} onDismiss={vi.fn()} />);
    expect(failed).toContain('value="https://employer.example"');
    expect(failed).toContain('Review website and discover');
    const completed = renderToStaticMarkup(<CompanyDiscoveryRequests rows={[{...row, status:'COMPLETED', selected_website:'https://employer.example'}]} busy={false} onReview={vi.fn()} onDismiss={vi.fn()} />);
    expect(completed).not.toContain('Review website and discover');
    expect(completed).toContain('needs no further action');
  });

  it('explains an unknown request without inventing a verification state', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryRequests rows={[{...row, status:'UNEXPECTED', reasons:[], reason:null}]} busy={true} onReview={vi.fn()} onDismiss={vi.fn()} />);
    expect(html).toContain('Status unavailable');
    expect(html).toContain('No reason recorded');
    expect(html).not.toContain('Verified');
  });
});
