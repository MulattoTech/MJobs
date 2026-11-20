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


describe('employer verification opt-in', () => {
  const label = 'Verify employer links from imported jobs';
  const active = { version:1, mode:'GREENHOUSE_RECIPROCAL_V1', activation_id:'a'.repeat(32) } as const;
  function inputs(value:CompanyDiscoveryPolicyValue, onValueChange = vi.fn(), disabled = false) {
    const result: React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>[] = [];
    const visit = (node:unknown) => {
      if (!React.isValidElement(node)) return;
      if (node.type === 'input') result.push(node as React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>);
      React.Children.forEach((node.props as { children?:React.ReactNode }).children, visit);
    };
    visit(CompanyDiscoveryPolicy({ value, onValueChange, disabled }));
    return result;
  }
  it('is visibly off by default and explains the saved public-read scope', () => {
    const control = inputs({}).find(input => input.props['aria-label'] === label)!;
    expect(control.props.checked).toBe(false);
    expect(control.props.disabled).toBe(false);
    const html = renderToStaticMarkup(<CompanyDiscoveryPolicy value={{}} onValueChange={vi.fn()}/>);
    expect(html).toContain(label);
    expect(html).toContain('Greenhouse links against the employer site after saving this profile');
    expect(html).toContain('existing collection limits still apply');
  });
  it('requests explicit activation without minting a client ID or changing other preferences', () => {
    const value = { minimum_match_for_discovery:65, priority_match_for_discovery:90 };
    const onValueChange = vi.fn();
    const control = inputs(value, onValueChange).find(input => input.props['aria-label'] === label)!;
    control.props.onChange?.({ currentTarget:{ checked:true } } as React.ChangeEvent<HTMLInputElement>);
    expect(onValueChange).toHaveBeenCalledWith({ ...value, employer_verification:{
      version:1, mode:'GREENHOUSE_RECIPROCAL_V1', activation_id:null,
    } });
  });
  it('revokes through the same value callback and preserves unrelated controls', () => {
    const value = { minimum_match_for_discovery:65, employer_verification:active };
    const onValueChange = vi.fn();
    const control = inputs(value, onValueChange).find(input => input.props['aria-label'] === label)!;
    expect(control.props.checked).toBe(true);
    control.props.onChange?.({ currentTarget:{ checked:false } } as React.ChangeEvent<HTMLInputElement>);
    expect(onValueChange).toHaveBeenCalledWith({ ...value, employer_verification:{
      version:1, mode:'DISABLED', activation_id:null,
    } });
  });
  it('clears the grant when company discovery is disabled', () => {
    const value = { company_discovery_enabled:true, employer_verification:active };
    const onValueChange = vi.fn();
    const control = inputs(value, onValueChange).find(input => input.props.type === 'checkbox'
      && input.props['aria-label'] !== label)!;
    control.props.onChange?.({ currentTarget:{ checked:false } } as React.ChangeEvent<HTMLInputElement>);
    expect(onValueChange).toHaveBeenCalledWith({ company_discovery_enabled:false, employer_verification:{
      version:1, mode:'DISABLED', activation_id:null,
    } });
  });
  it('cannot display an active grant while discovery is disabled and respects saving state', () => {
    const control = inputs({ company_discovery_enabled:false, employer_verification:active })
      .find(input => input.props['aria-label'] === label)!;
    expect(control.props.checked).toBe(false);
    expect(control.props.disabled).toBe(true);
    expect(inputs({ employer_verification:active }, vi.fn(), true)
      .find(input => input.props['aria-label'] === label)!.props.disabled).toBe(true);
  });
  it('does not expose the activation marker in the rendered controls', () => {
    const html = renderToStaticMarkup(<CompanyDiscoveryPolicy value={{ employer_verification:active }} onValueChange={vi.fn()}/>);
    expect(html).not.toContain(active.activation_id);
  });
});
