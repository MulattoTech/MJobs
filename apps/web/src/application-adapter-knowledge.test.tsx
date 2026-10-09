import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ApplicationAdapterKnowledgePanel } from './application-adapter-knowledge';

describe('application adapter knowledge', () => {
  it('distinguishes company-level knowledge from a verified application route', () => {
    const html = renderToStaticMarkup(<ApplicationAdapterKnowledgePanel knowledge={{
      provider: 'greenhouse', tenant_identifier: 'acme', basis: 'COMPANY_DISCOVERY',
      adapter_status: 'UNSUPPORTED', adapter_key: null, external_submission_enabled: false,
    }} />);

    expect(html).toContain('Greenhouse');
    expect(html).toContain('acme');
    expect(html).toContain('Company discovery');
    expect(html).toContain('current application URL has not been verified');
    expect(html).toContain('No supported MJobs application adapter is available');
    expect(html).toContain('External application submission remains OFF');
  });

  it('identifies an exact discovered mock route as fixture-only', () => {
    const html = renderToStaticMarkup(<ApplicationAdapterKnowledgePanel knowledge={{
      provider: 'mock-ats', tenant_identifier: 'fixture', basis: 'ROUTE_AND_DISCOVERY',
      adapter_status: 'FIXTURE_ONLY', adapter_key: 'mock-ats',
      external_submission_enabled: false,
    }} />);

    expect(html).toContain('Selected route matches saved ATS discovery evidence');
    expect(html).toContain('Fictional local mock adapter');
    expect(html).toContain('fixture only');
    expect(html).toContain('contact an employer');
  });

  it('does not choose among ambiguous ATS discovery candidates', () => {
    const html = renderToStaticMarkup(<ApplicationAdapterKnowledgePanel knowledge={{
      provider: null, tenant_identifier: null, basis: 'AMBIGUOUS',
      adapter_status: 'REVIEW_REQUIRED', adapter_key: null,
      external_submission_enabled: false,
    }} />);

    expect(html).toContain('More than one ATS identity is available');
    expect(html).toContain('MJobs did not choose a provider or adapter');
    expect(html).toContain('needs review');
  });

  it('shows missing knowledge as unknown without implying adapter support', () => {
    const html = renderToStaticMarkup(<ApplicationAdapterKnowledgePanel />);

    expect(html).toContain('UNKNOWN');
    expect(html).toContain('No supported application adapter is confirmed');
    expect(html).toContain('External application submission remains OFF');
  });
});
