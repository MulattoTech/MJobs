import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { CompanyDiscoveryPolicy, type CompanyDiscoveryPolicyValue } from './company-discovery-policy';

describe('company discovery policy settings', () => {
  it('shows safe defaults and distinguishes discovery ordering from application submission', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryPolicy value={{}} onValueChange={vi.fn()} />);
    expect(html).toContain('Enable company discovery');
    expect(html).toContain('Minimum match score for discovery');
    expect(html).toContain('Match score for priority discovery');
    expect(html).toContain('Maximum new company discoveries per hour');
    expect(html).toContain('value="60"');
    expect(html).toContain('value="80"');
    expect(html).toContain('value="12"');
    expect(html).toContain('never submits applications');
  });

  it('updates only the selected policy value and preserves the other fields', () => {
    const onValueChange = vi.fn();
    const value: CompanyDiscoveryPolicyValue = {
      company_discovery_enabled: false,
      minimum_match_for_discovery: 55,
      priority_match_for_discovery: 85,
      max_new_company_discoveries_per_hour: 6,
    };
    const tree = CompanyDiscoveryPolicy({ value, onValueChange });
    const inputs: React.ReactElement[] = [];
    const visit = (node: unknown) => {
      if (!React.isValidElement(node)) return;
      if (node.type === 'input') inputs.push(node);
      React.Children.forEach((node.props as { children?: React.ReactNode }).children, visit);
    };
    visit(tree);
    const minimum = inputs.find(input => (input.props as React.InputHTMLAttributes<HTMLInputElement>)['aria-label'] === 'Minimum match score for discovery');
    expect(minimum).toBeDefined();
    (minimum!.props as React.InputHTMLAttributes<HTMLInputElement>).onChange?.({ currentTarget:{ value:'65' } } as React.ChangeEvent<HTMLInputElement>);
    expect(onValueChange).toHaveBeenCalledWith({ ...value, minimum_match_for_discovery:65 });
  });

  it('disables controls when profile saving is unavailable', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryPolicy value={{}} onValueChange={vi.fn()} disabled />);
    expect(html).toContain('type="checkbox" disabled=""');
    expect(html).toContain('aria-label="Minimum match score for discovery" type="number" min="0" max="80" disabled=""');
  });
});
