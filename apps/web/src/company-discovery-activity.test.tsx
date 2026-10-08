import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { discoveryActivity } from './company-activity';
import { CompanyDiscoveryActivity } from './company-discovery-activity';
import type { DiscoveryActivity } from './company-activity';

function findButtons(node: unknown): React.ReactElement<React.ButtonHTMLAttributes<HTMLButtonElement>>[] {
  if (!React.isValidElement(node)) return [];
  const found = node.type === 'button' ? [node as React.ReactElement<React.ButtonHTMLAttributes<HTMLButtonElement>>] : [];
  const children = (node.props as {children?: React.ReactNode}).children;
  return [...found, ...React.Children.toArray(children).flatMap(findButtons)];
}

describe('CompanyDiscoveryActivity', () => {
  it('renders known event summaries and dates as normal user-facing activity', () => {
    const rows = discoveryActivity([
      {id: 'company-1', event_type: 'COMPANY_DISCOVERED', summary: 'Discovered Acme from Remote Rocketship', timestamp: '2026-10-08T12:00:00Z'},
      {id: 'domain-1', event_type: 'COMPANY_DOMAIN_RESOLVED', summary: 'Resolved official domain', timestamp: '2026-10-08T11:00:00Z'},
      {id: 'ats-1', event_type: 'COMPANY_ATS_DETECTED', summary: 'Detected Lever', timestamp: '2026-10-08T10:00:00Z'},
      {id: 'source-1', event_type: 'COMPANY_SOURCE_ENROLLED', summary: 'Added a direct employer source', timestamp: '2026-10-08T09:00:00Z'},
      {id: 'import-1', event_type: 'FEED_IMPORT_COMPLETE', summary: 'Imported 47 active openings', timestamp: '2026-10-08T08:00:00Z'},
      {id: 'matches-1', event_type: 'COMPANY_FEED_MATCHES', summary: '6 openings match this profile', timestamp: '2026-10-08T07:00:00Z'},
    ]);
    const html = renderToStaticMarkup(<CompanyDiscoveryActivity rows={rows} loading={false} error={false}/>);

    expect(html).toContain('Discovered Acme from Remote Rocketship');
    expect(html).toContain('Resolved official domain');
    expect(html).toContain('Detected Lever');
    expect(html).toContain('Added a direct employer source');
    expect(html).toContain('Imported 47 active openings');
    expect(html).toContain('6 openings match this profile');
    expect(html).toContain('<time dateTime="2026-10-08T12:00:00.000Z">');
    expect(html.indexOf('Discovered Acme')).toBeLessThan(html.indexOf('Resolved official domain'));
  });

  it('shows loading and empty states without exposing stale rows', () => {
    const staleRows: DiscoveryActivity[] = [{id: 'old', message: 'Old result', timestamp: null}];
    const loading = renderToStaticMarkup(<CompanyDiscoveryActivity rows={staleRows} loading={true} error={false}/>);
    expect(loading).toContain('role="status"');
    expect(loading).toContain('Loading company discovery activity');
    expect(loading).not.toContain('Old result');
    expect(loading).not.toContain('No company discovery activity');

    const empty = renderToStaticMarkup(<CompanyDiscoveryActivity rows={[]} loading={false} error={false}/>);
    expect(empty).toContain('No company discovery activity has been recorded yet.');
  });

  it('shows an honest error state and invokes retry when selected', () => {
    const onRetry = vi.fn();
    const tree = CompanyDiscoveryActivity({rows: [], loading: false, error: true, onRetry});
    const html = renderToStaticMarkup(tree);
    expect(html).toContain('role="alert"');
    expect(html).toContain('Company discovery activity could not be loaded. Try again.');
    expect(html).toContain('Retry activity');

    const retry = findButtons(tree)[0];
    expect(retry).toBeDefined();
    retry!.props.onClick?.({} as React.MouseEvent<HTMLButtonElement>);
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('renders only normalized summaries, never arbitrary audit or private payload text', () => {
    const rows = discoveryActivity([
      {
        id: 'safe', event_type: 'COMPANY_DISCOVERED',
        summary: 'Discovered Acme', timestamp: null,
        details: {raw: 'PRIVATE_AUDIT_PAYLOAD', endpoint: 'https://example.test/?token=PRIVATE_TOKEN'},
        debug: 'PRIVATE_DEBUG_STRING',
      },
      {id: 'unknown', event_type: 'PRIVATE_AUDIT_EVENT', summary: 'PRIVATE_UNKNOWN_SUMMARY', timestamp: null},
      {id: 'unsafe', event_type: 'COMPANY_DISCOVERED', summary: 'PRIVATE_SUMMARY\nraw details', timestamp: null},
    ]);
    const html = renderToStaticMarkup(<CompanyDiscoveryActivity rows={rows} loading={false} error={false}/>);

    expect(html).toContain('Discovered Acme');
    expect(html).not.toContain('PRIVATE_AUDIT_PAYLOAD');
    expect(html).not.toContain('PRIVATE_TOKEN');
    expect(html).not.toContain('PRIVATE_DEBUG_STRING');
    expect(html).not.toContain('PRIVATE_UNKNOWN_SUMMARY');
    expect(html).not.toContain('PRIVATE_SUMMARY');
    expect(html).not.toContain('PRIVATE_AUDIT_EVENT');
  });
});
