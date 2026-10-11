import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it, vi} from 'vitest';
import {CompanyDiscoveryQueue, type CompanyDiscoveryQueueItem, type CompanyDiscoveryQueueCounts} from './company-discovery-queue';

const items: CompanyDiscoveryQueueItem[] = [
  {request_id:'request-queued', company:'Northwind Systems', state:'QUEUED', priority:'NORMAL', updated_at:'2026-10-10T08:00:00Z', attempt_count:0},
  {request_id:'request-running', company:'Fabrikam', state:'RUNNING', priority:'NORMAL', updated_at:'2026-10-10T08:01:00Z', attempt_count:1},
  {request_id:'request-succeeded', company:'Contoso', state:'SUCCEEDED', priority:'HIGH', updated_at:'2026-10-10T08:02:00Z', attempt_count:1},
  {request_id:'request-review', company:'Adventure Works', state:'NEEDS_REVIEW', priority:'NORMAL', updated_at:'2026-10-10T08:03:00Z', attempt_count:3},
  {request_id:'request-cancelled', company:'Tailspin Toys', state:'CANCELLED', priority:'NORMAL', updated_at:'2026-10-10T08:04:00Z', attempt_count:0},
  {request_id:'request-unknown', company:'Wide World Importers', state:'UNKNOWN', priority:'NORMAL', updated_at:null, attempt_count:0},
];
const counts: CompanyDiscoveryQueueCounts = {total:6, queued:1, running:1, succeeded:1, needs_review:1, cancelled:1, unknown:1};

const render = (overrides: Partial<React.ComponentProps<typeof CompanyDiscoveryQueue>> = {}) => renderToStaticMarkup(
  <CompanyDiscoveryQueue profileId="profile-a" items={items} counts={counts} loading={false} error={false} {...overrides}/>,
);

describe('company discovery queue', () => {
  it('shows safe request summaries and maps every stable service state to a readable label', () => {
    const html = render();
    for (const value of [
      'Northwind Systems','Queued','Fabrikam','Running','Contoso','Completed',
      'Adventure Works','Needs review','Tailspin Toys','Cancelled',
      'Wide World Importers','Status unavailable','Standard priority','High priority',
    ]) expect(html).toContain(value);
    for (const value of ['Total</dt><dd>6','Queued</dt><dd>1','Running</dt><dd>1','Succeeded</dt><dd>1','Needs review</dt><dd>1','Cancelled</dt><dd>1','Unknown</dt><dd>1']) expect(html).toContain(value);
    expect(html).not.toContain('Retry discovery');
  });

  it('offers refresh only when a read refresh callback is supplied', () => {
    expect(render()).not.toContain('Refresh company discovery');
    expect(render({onRefresh:vi.fn()})).toContain('Refresh company discovery');
  });

  it('shows a loading state without presenting an empty-state claim', () => {
    const html = render({loading:true, items:[], counts:{total:0, queued:0, running:0, succeeded:0, needs_review:0, cancelled:0, unknown:0}});
    expect(html).toContain('Loading company discovery activity');
    expect(html).not.toContain('No company discoveries are queued');
  });

  it('shows an empty state only when the profile has no queued history', () => {
    const empty = {total:0, queued:0, running:0, succeeded:0, needs_review:0, cancelled:0, unknown:0};
    expect(render({items:[], counts:empty})).toContain('No company discoveries are queued for this profile.');
    expect(render({items:[], counts:{...empty, total:1, unknown:1}})).toContain('No recent company discovery requests are available for this profile.');
  });

  it('shows a safe generic error without exposing raw provider or operation payloads', () => {
    const itemWithRawPayload = {...items[0], raw_payload:'{"operation_id":"private-db-id","credential":"secret","html":"<script>leak()</script>"}'} as CompanyDiscoveryQueueItem;
    const html = render({error:true, items:[itemWithRawPayload]});
    expect(html).toContain('Company discovery activity could not be loaded.');
    for (const value of ['private-db-id','secret','<script>','raw_payload']) expect(html).not.toContain(value);
  });

  it('does not render queue data when no profile is selected', () => {
    const html = render({profileId:'', items});
    expect(html).toContain('Select a profile to view company discovery activity.');
    expect(html).not.toContain('Northwind Systems');
  });
});
