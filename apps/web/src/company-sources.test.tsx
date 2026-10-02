import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

const modules = import.meta.glob<{CompanySources: React.ComponentType<any>}>('./company-sources.tsx');
async function render(rows: unknown[]) {
  expect(modules['./company-sources.tsx'], 'A real Companies/Sources component is required').toBeTypeOf('function');
  const { CompanySources } = await modules['./company-sources.tsx']();
  return renderToStaticMarkup(<CompanySources rows={rows} busy={false} profileSelected={true} onSet={async()=>{}} />);
}
const source = {id:'source-1', company_id:'acme', company_name:'Acme', official_domain:null,
  career_url:'https://careers.example/jobs', provider:'rss', tenant:'acme', collection_strategy:'RSS',
  confidence:0.45, priority:'NORMAL', enabled:true, origin:'AUTO_DISCOVERED', discovered_automatically:true,
  status:'COMPANY_SOURCE_REVIEW_REQUIRED', jobs_found:2, relevant_jobs:1, last_checked:null, next_check:null,
  provenance:{events:[], tenant_evidence:[], source_linked_history_available:false}};

describe('Companies/Sources view', () => {
  it('renders evidence-based fields, real controls and explicit unavailable actions', async () => {
    const html=await render([source]);
    for (const label of ['Company','Official Domain','Careers URL','ATS','Source Type','Confidence','Priority',
      'Status','Jobs Found','Relevant Jobs','Last Checked','Next Check','Not scheduled','Not verified',
      'Pin','Disable','Watch','View Evidence','Run Now','Rediscover','Verify','Edit','unavailable']) expect(html).toContain(label);
    expect(html).toContain('COMPANY_SOURCE_REVIEW_REQUIRED');
    expect(html).toContain('45%');
    expect(html).toContain('data-source-id="source-1"');
  });
  it('escapes external names and refuses unsafe career links', async () => {
    const html=await render([{...source, company_name:'<script>unsafe()</script>', career_url:'javascript:alert(1)'}]);
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('href="javascript:');
    expect(html).toContain('&lt;script&gt;');
  });
  it('shows an honest empty state', async () => {
    expect(await render([])).toContain('No company sources enrolled');
  });
});
