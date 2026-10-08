import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationCapabilityFilter } from './application-capability-filter';

describe('application capability filter', () => {
  it('offers truthful distinct workspace adapter capability values', () => {
    const html = renderToStaticMarkup(
      <ApplicationCapabilityFilter value="" onChange={() => {}} />,
    );

    expect(html).toContain('<label for="application-capability">Application adapter capability (this workspace)</label>');
    expect(html).toContain('<option value="" selected="">All capabilities</option>');
    expect(html).toContain('<option value="FIXTURE_ONLY">Fixture-only support</option>');
    expect(html).toContain('<option value="UNSUPPORTED">Unsupported</option>');
    expect(html).toContain('<option value="REVIEW_REQUIRED">Review required</option>');
    expect(html).toContain('<option value="UNKNOWN">Unknown</option>');
    expect(html).toContain("Reports this workspace's existing adapter capability");
    expect(html).toContain('makes no employer requests');
  });

  it('reflects the controlled value and disabled state', () => {
    const html = renderToStaticMarkup(
      <ApplicationCapabilityFilter value="REVIEW_REQUIRED" onChange={() => {}} disabled />,
    );

    expect(html).toContain('aria-label="Application adapter capability (this workspace)" disabled=""');
    expect(html).toContain('<option value="REVIEW_REQUIRED" selected="">Review required</option>');
  });

  it('leaves changes to the owning search form', () => {
    const onChange = vi.fn();
    renderToStaticMarkup(
      <ApplicationCapabilityFilter value="FIXTURE_ONLY" onChange={onChange} />,
    );

    expect(onChange).not.toHaveBeenCalled();
  });
});
