import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { CompanyDiscovery } from './company-discovery';

describe('company discovery form', () => {
  it('explains public-only discovery and renders required HTTPS inputs', () => {
    const html = renderToStaticMarkup(<CompanyDiscovery busy={false} onDiscover={vi.fn()} />);
    expect(html).toContain('Add a company to look for jobs');
    expect(html).toContain('public pages only');
    expect(html).toContain('stays unverified');
    expect(html).toContain('Other recruiting systems may need review');
    expect(html).toContain('pattern="https://.*"');
    expect(html).toContain('Discover company jobs');
  });

  it('disables discovery while work is in progress', () => {
    const html = renderToStaticMarkup(<CompanyDiscovery busy={true} onDiscover={vi.fn()} />);
    expect((html.match(/disabled=""/g) || []).length).toBe(3);
  });
});
