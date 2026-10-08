import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationLinkFilter } from './application-link-filter';

describe('application link availability filter', () => {
  it('renders a labeled, keyboard-accessible select and explains availability semantics', () => {
    const html = renderToStaticMarkup(
      <ApplicationLinkFilter value="" onChange={() => {}} />,
    );

    expect(html).toContain('<label for="application-link-availability">Application link availability</label>');
    expect(html).toContain('id="application-link-availability" aria-label="Application link"');
    expect(html).toContain('<option value="" selected="">All availability</option>');
    expect(html).toContain('<option value="AVAILABLE">Available</option>');
    expect(html).toContain('<option value="UNAVAILABLE">Unavailable</option>');
    expect(html).toContain('Unavailable means no application link was reported.');
    expect(html).toContain('Unknown application link states are excluded when a filter is selected.');
    expect(html).not.toMatch(/employer received|ATS support|submit application/i);
  });

  it('reflects the selected status and supports a disabled state', () => {
    const html = renderToStaticMarkup(
      <ApplicationLinkFilter value="UNAVAILABLE" onChange={() => {}} disabled />,
    );

    expect(html).toContain('aria-label="Application link" disabled=""');
    expect(html).toContain('<option value="UNAVAILABLE" selected="">Unavailable</option>');
  });

  it('leaves user changes to the owning search form', () => {
    const onChange = vi.fn();
    const html = renderToStaticMarkup(<ApplicationLinkFilter value="AVAILABLE" onChange={onChange} />);

    expect(html).toContain('<option value="AVAILABLE" selected="">Available</option>');
    expect(onChange).not.toHaveBeenCalled();
  });
});
