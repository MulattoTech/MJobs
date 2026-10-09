import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationRoutePreference } from './application-route-preference';

describe('application route preference settings', () => {
  it('offers verified-first and explicit per-job route choice without claiming provider names are proof', () => {
    const html = renderToStaticMarkup(<ApplicationRoutePreference value="PREFER_VERIFIED" onValueChange={vi.fn()} />);
    expect(html).toContain('Prefer a verified employer or ATS route when evidence supports it');
    expect(html).toContain('Ask me to choose a route for each job');
    expect(html).toContain('A provider name alone does not verify a route.');
    expect(html).toContain('name="application-route-preference" checked="" value="PREFER_VERIFIED"');
  });

  it('reports explicit profile choice and respects the saving gate', () => {
    const onValueChange = vi.fn();
    const html = renderToStaticMarkup(<ApplicationRoutePreference value="USER_CHOICE" onValueChange={onValueChange} disabled />);
    expect(html).toContain('disabled="" name="application-route-preference" checked="" value="USER_CHOICE"');
  });
});
