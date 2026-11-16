import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { SourceHealthDashboard, type GlobalSourceHealth, type CompanySourceHealth } from './source-health-dashboard';

const globalSource: GlobalSourceHealth = {
  id: 'feed-a', name: 'Public Jobs', provider: 'remotive', strategy: 'PUBLIC_JSON',
  enabled: true, health_status: 'HEALTHY', last_success: '2026-11-15T12:00:00Z',
  last_attempt: '2026-11-15T12:00:00Z', consecutive_failures: 0, jobs_seen: 24,
  backoff_until: null, last_error: null,
};

const companySource: CompanySourceHealth = {
  id: 'employer-a', company_name: 'Example Employer', provider: 'greenhouse',
  collection_strategy: 'ATS_API', enabled: true, status: 'HEALTHY',
  jobs_found: 7, last_success: '2026-11-15T13:00:00Z',
  next_check: '2026-11-16T13:00:00Z',
};

function render(globals: GlobalSourceHealth[], companies: CompanySourceHealth[]) {
  return renderToStaticMarkup(<SourceHealthDashboard globalSources={globals} companySources={companies}/>);
}

describe('source health dashboard', () => {
  it('shows separate global/company tables with every named metric and a truthful unknown', () => {
    const html = render([globalSource], [companySource]);
    for (const header of ['Source', 'Type', 'Strategy', 'Health', 'Last Success',
      'Last Failure', 'Jobs Seen', 'New Jobs', 'Duplicates', 'Duration', 'Next Run']) {
      expect(html).toContain('<th scope="col">' + header + '</th>');
    }
    expect(html).toContain('Global Sources');
    expect(html).toContain('Company Sources');
    expect(html).toContain('Public Jobs');
    expect(html).toContain('Example Employer');
    expect(html).toContain('24');
    expect(html).toContain('7 (currently found)');
    expect(html).toContain('Not recorded');
    expect(html).toContain('Unmeasured fields are not zero');
  });

  it('uses only verified failure timestamps, never raw error details or secret API fields', () => {
    const html = render([Object.assign({
      ...globalSource, health_status: 'DEGRADED', consecutive_failures: 2,
      last_attempt: '2026-11-16T02:00:00Z',
      last_error: 'PRIVATE_DETAIL_DO_NOT_PRINT',
    }, {endpoint: 'https://private.example/token'})], [{
      ...companySource, status: 'FAILED', monitoring: {
        revision: 1, enabled: true, poll_interval: 3600,
        status: 'FAILED', failure_count: 1, last_attempt: '2026-11-16T03:00:00Z',
        next_due: null, last_error: 'SECRET_FAILURE', operation_id: null,
        rediscovery_operation_id: null, authorization: null,
      },
    }]);
    expect(html).toContain('2026-11-16 02:00 UTC');
    expect(html).toContain('2026-11-16 03:00 UTC');
    expect(html).not.toContain('PRIVATE_DETAIL_DO_NOT_PRINT');
    expect(html).not.toContain('SECRET_FAILURE');
    expect(html).not.toContain('private.example');
  });

  it('keeps disabled sources paused and never invents scheduled execution', () => {
    const html = render([{
      ...globalSource, enabled: false, backoff_until: '2026-11-18T12:00:00Z',
    }], [{...companySource, enabled: false, next_check: '2026-11-18T12:00:00Z'}]);
    expect(html).toContain('Disabled');
    expect(html.match(/Paused/g)?.length).toBe(2);
    expect(html).not.toContain('2026-11-18');
  });

  it('handles empty source groups without implying successful collection', () => {
    const html = render([], []);
    expect(html).toContain('No global sources configured.');
    expect(html).toContain('No company sources enrolled.');
    expect(html).not.toContain('0 jobs collected');
  });

  it('rejects invalid timestamps and escapes untrusted source names', () => {
    const html = render([{
      ...globalSource, name: '<script>danger</script>', last_success: 'bad-time',
      last_attempt: null, jobs_seen: Number.NaN,
    }], []);
    expect(html).toContain('&lt;script&gt;danger&lt;/script&gt;');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('NaN');
    expect(html).toContain('Not recorded');
  });
});
