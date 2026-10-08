import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { JobRouteProvenance } from './job-route-provenance';

describe('job route provenance', () => {
  it('labels discovery source and application provider separately', () => {
    const html = renderToStaticMarkup(<JobRouteProvenance
      postingSources={['HiringCafe']}
      routeProvider="Greenhouse"
    />);

    expect(html).toContain('DISCOVERY SOURCE');
    expect(html).toContain('HiringCafe');
    expect(html).toContain('APPLICATION PROVIDER');
    expect(html).toContain('Greenhouse');
    expect(html.indexOf('DISCOVERY SOURCE')).toBeLessThan(html.indexOf('HiringCafe'));
    expect(html.indexOf('APPLICATION PROVIDER')).toBeLessThan(html.indexOf('Greenhouse'));
  });

  it('preserves each distinct discovery source for a posting', () => {
    const html = renderToStaticMarkup(<JobRouteProvenance
      postingSources={['HiringCafe', 'Greenhouse']}
      routeProvider="Lever"
    />);

    expect(html).toContain('HiringCafe');
    expect(html).toContain('Greenhouse');
    expect(html).toContain('Lever');
  });

  it('shows an explicit empty state when discovery sources are empty', () => {
    const html = renderToStaticMarkup(<JobRouteProvenance postingSources={[]} routeProvider="Greenhouse" />);

    expect(html).toContain('DISCOVERY SOURCE');
    expect(html).toContain('Not recorded');
    expect(html).toContain('APPLICATION PROVIDER');
    expect(html).toContain('Greenhouse');
  });

  it.each([undefined, null, '', 'https://[invalid'])('shows a safe unknown application provider for %s', routeProvider => {
    const html = renderToStaticMarkup(<JobRouteProvenance postingSources={['HiringCafe']} routeProvider={routeProvider} />);

    expect(html).toContain('APPLICATION PROVIDER');
    expect(html).toContain('Unknown');
    expect(html).not.toContain('undefined');
    expect(html).not.toContain('null');
  });

  it('states that a recorded route provider does not verify ATS identity', () => {
    const html = renderToStaticMarkup(<JobRouteProvenance
      postingSources={['HiringCafe']}
      routeProvider="Greenhouse"
    />);

    expect(html).toMatch(/recorded route provider.*does not prove that ATS support has been verified/i);
  });
});
