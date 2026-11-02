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
  it('shows declared support separately from unsupported actions and enabled state', async () => {
    const capabilities = {strategy:'FEED', capabilities:['DISCOVERY','JOB_DETAIL','SALARY','REMOTE_METADATA','APPLICATION_REDIRECT'],
      discovery:true, job_detail:true, salary:true, remote_metadata:true, application_redirect:true,
      direct_application:false, application_status:false};
    const html = await render([{...source, enabled:false, capabilities}]);
    expect(html).toContain('Collection method: FEED');
    expect(html).toContain('Supported: Job discovery, Job details, Salary extraction, Remote-work metadata, Application links');
    expect(html).toContain('Unsupported: Direct application, Application status updates');
    expect(html).toContain('Pinned · Disabled · Watched');
  });
  it('keeps unsupported sources distinct from declarations missing in older readbacks', async () => {
    const capabilities = {strategy:'DISABLED', capabilities:[], discovery:false, job_detail:false, salary:false,
      remote_metadata:false, application_redirect:false, direct_application:false, application_status:false};
    const html = await render([{...source, capabilities}]);
    expect(html).toContain('Supported: None declared');
    expect(html).toContain('Unsupported: Job discovery, Job details, Salary extraction, Remote-work metadata, Application links, Direct application, Application status updates');
    expect(await render([source])).toContain('Capabilities have not been declared for this source.');
  });
  it('labels the local mock source so its declaration cannot imply public support', async () => {
    const html = await render([{...source, provider:'mock-ats', collection_strategy:'FIXTURE',
      capabilities:{strategy:'API', capabilities:['DISCOVERY','JOB_DETAIL','SALARY','REMOTE_METADATA','APPLICATION_REDIRECT'],
        discovery:true, job_detail:true, salary:true, remote_metadata:true, application_redirect:true,
        direct_application:false, application_status:false}}]);
    expect(html).toContain('Local fixture source.');
    expect(html).toContain('Collection method: API');
    expect(html).toContain('Unsupported: Direct application, Application status updates');
  });
  it('distinguishes user-confirmed recurring identity from independent verification', async () => {
    const html = await render([{...source, collector: {
      status:'ACTIVE', poll_interval:300, identity_basis:'USER_CONFIRMED_DOMAIN',
    }}]);
    expect(html).toContain('User-confirmed employer identity');
    expect(html).toContain('Not independently verified');
  });
  it('shows the persisted automatic collector and its paused state', async () => {
    const html = await render([{...source, collector: {status:'PAUSED', poll_interval:900}}]);
    expect(html).toContain('Automatic collection');
    expect(html).toContain('Paused');
    expect(html).toContain('900 seconds');
  });
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
  it('labels user-entered company context and keeps unverified context out of letters', async () => {
    const html = await render([{...source, company_contexts:[{
      id:'context-1', version:1, content:'Acme has regional operations.', content_origin:'USER_PROVIDED', content_status:'UNVERIFIED',
      source_url:'https://careers.example/about', identity_status:'UNVERIFIED',
      content_sha256:'a'.repeat(64), user_reviewed:false, user_approved:false,
    }]}]);
    expect(html).toContain('Context is user-provided and unverified');
    expect(html).toContain('USER_PROVIDED');
    expect(html).toContain('CONTENT UNVERIFIED');
    expect(html).toContain('UNVERIFIED');
    expect(html).toContain('Unverified source identity; this context will not be used in letters.');
    expect(html).toContain('Context text from the source');
    expect(html).toContain('Source URL');
    expect(html).toContain('Save user-provided context');
  });
});
