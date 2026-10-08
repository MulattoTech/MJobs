import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

const modules = import.meta.glob<{CompanySources: React.ComponentType<any>}>('./company-sources.tsx');
async function render(rows: unknown[]) {
  expect(modules['./company-sources.tsx'], 'A real Companies/Sources component is required').toBeTypeOf('function');
  const { CompanySources } = await modules['./company-sources.tsx']();
  return renderToStaticMarkup(<CompanySources rows={rows} busy={false} profileSelected={true} onSet={async()=>{}} />);
}
const source = {id:'source-1', company_id:'acme', profile_id:'fixture', profile_company_associated:true, company_name:'Acme', official_domain:null,
  career_url:'https://careers.example/jobs', provider:'rss', tenant:'acme', collection_strategy:'RSS',
  confidence:0.45, priority:'USER_PINNED', enabled:true, origin:'USER_WATCHED', discovered_automatically:true,
  status:'COMPANY_SOURCE_REVIEW_REQUIRED', jobs_found:2, relevant_jobs:1, applications:3, last_checked:null, next_check:null,
  provenance:{events:[], tenant_evidence:[], source_linked_history_available:false}};

describe('Companies/Sources view', () => {
  it('renders evidence-based fields, real controls and explicit unavailable actions', async () => {
    const html=await render([source]);
    for (const label of ['Company','Domain','Career Site','ATS','Source Priority','Relevant Jobs','Applications','User Preference',
      'Status','Jobs Found','Last Checked','Next Check','Not scheduled','Not verified',
      'Pin','Disable','Watch','View Evidence','Run Now','Rediscover','Verify','Edit','unavailable']) expect(html).toContain(label);
    expect(html).toContain('Company notes');
    expect(html).toContain('COMPANY_SOURCE_REVIEW_REQUIRED');
    expect(html).toContain('45%');
    expect(html).toContain('>3</dd>');
    expect(html).toContain('Pinned · Enabled · Watched');
    expect(html).toContain('data-source-id="source-1"');
  });
  it('escapes external names and refuses unsafe career links', async () => {
    const html=await render([{...source, company_name:'<script>unsafe()</script>', career_url:'javascript:alert(1)'}]);
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('href="javascript:');
    expect(html).toContain('&lt;script&gt;');
  });
  it('shows readable discovery history before the raw evidence details', async () => {
    const html=await render([{...source, provenance:{...source.provenance, source_linked_history_available:true,
      events:[{id:'import',action:'COMPANY_FEED_IMPORT_COMPLETE',timestamp:'2026-10-02T10:00:00Z',details:{jobs_imported:3,jobs_enumerated:5}}]}}]);
    expect(html).toContain('Recent discovery');
    expect(html).toContain('Imported 3 jobs from 5 feed records.');
    expect(html.indexOf('Imported 3 jobs')).toBeLessThan(html.indexOf('View Evidence'));
  });
  it('shows an honest empty state', async () => {
    expect(await render([])).toContain('No company sources enrolled');
  });
});
