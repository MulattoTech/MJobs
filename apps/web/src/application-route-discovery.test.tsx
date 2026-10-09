import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationRouteDiscovery, safeCandidateUrl } from './application-route-discovery';

describe('application route discovery', () => {
  it('offers a user-triggered check and explains that the saved route stays selected', () => {
    const onCheck = vi.fn(async () => ({source_route_id:'aggregator', status:'UNRESOLVED' as const}));
    const html = renderToStaticMarkup(<ApplicationRouteDiscovery
      routeId="aggregator" routeLabel="job-board · board" onCheck={onCheck}/>);
    expect(html).toContain('Check employer route');
    expect(html).toContain('Check only when requested');
    expect(html).toContain('never changes the selected route or submits');
    expect(onCheck).not.toHaveBeenCalled();
  });

  it('does not render a candidate from an unsafe scheme, query string, or fragment', () => {
    expect(safeCandidateUrl('javascript:alert(1)')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job?token=secret')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job#personal')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job')).toBe('https://ats.example/job');
  });
});
