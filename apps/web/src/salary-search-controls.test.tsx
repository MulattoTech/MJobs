import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  SalarySearchControls,
  DEFAULT_SALARY_SELECTION,
  applySalarySearchParams,
  readSalarySelection,
  salarySearchUrl,
  type SalarySelection,
} from './salary-search-controls';

describe('salary search controls', () => {
  it('retains legacy all-jobs defaults and annual USD labels', () => {
    const html = renderToStaticMarkup(
      <SalarySearchControls {...DEFAULT_SALARY_SELECTION}
        onMinChange={() => {}} onMaxChange={() => {}} onPeriodChange={() => {}}
        onPolicyChange={() => {}} />,
    );
    expect(html).toContain('Minimum annual salary (USD)');
    expect(html).toContain('Maximum annual salary (USD)');
    expect(html).toContain('Salary availability');
    expect(html).toContain('Salary unit');
    expect(html).toContain('Salary sorting still compares annual USD only');
    const params = new URLSearchParams({ q: 'remote' });
    applySalarySearchParams(params, DEFAULT_SALARY_SELECTION);
    expect(params.toString()).toBe('q=remote');
  });

  it('writes an explicit hourly unit and unknown-pay choice to the API', () => {
    const selected: SalarySelection = {
      min: '40.50', max: '70', period: 'HOUR', policy: 'ALLOW_UNKNOWN',
    };
    const params = new URLSearchParams({ sort: 'salary_high' });
    applySalarySearchParams(params, selected);
    expect(params.get('salary_min')).toBe('40.50');
    expect(params.get('salary_max')).toBe('70');
    expect(params.get('salary_period')).toBe('HOUR');
    expect(params.get('salary_unknown_allowed')).toBe('true');
    expect(params.has('salary_known_only')).toBe(false);
    expect(params.has('salary_policy')).toBe(false);
    const html = renderToStaticMarkup(
      <SalarySearchControls {...selected}
        onMinChange={() => {}} onMaxChange={() => {}} onPeriodChange={() => {}}
        onPolicyChange={() => {}} />,
    );
    expect(html).toContain('Minimum hourly salary (USD)');
    expect(html).toContain('Changing salary unit clears entered bounds');
  });

  it('requires disclosed salary when chosen, even without numeric bounds', () => {
    const params = new URLSearchParams();
    applySalarySearchParams(params, {
      ...DEFAULT_SALARY_SELECTION, policy: 'KNOWN_ONLY',
    });
    expect(params.get('salary_period')).toBe('YEAR');
    expect(params.get('salary_known_only')).toBe('true');
    expect(params.has('salary_unknown_allowed')).toBe(false);
  });

  it('round trips URL filters through reload while retaining unrelated state', () => {
    const selected: SalarySelection = {
      min: '35', max: '60.75', period: 'HOUR', policy: 'ALLOW_UNKNOWN',
    };
    const url = salarySearchUrl('https://mjobs.example.test/?tab=Jobs#details', selected);
    expect(url).toContain('tab=Jobs');
    expect(url).toContain('salary_period=HOUR');
    expect(url).toContain('salary_policy=ALLOW_UNKNOWN');
    const parsed = readSalarySelection(new URL(url, 'https://mjobs.example.test').search);
    expect(parsed).toEqual(selected);
    const reset = salarySearchUrl('https://mjobs.example.test' + url, DEFAULT_SALARY_SELECTION);
    expect(reset).toBe('/?tab=Jobs#details');
  });

  it('rejects invalid URL salaries and unrecognized units/policies on reload', () => {
    expect(readSalarySelection('?salary_min=-2&salary_max=100000001&salary_period=MONTH&salary_policy=ESTIMATE'))
      .toEqual(DEFAULT_SALARY_SELECTION);
    expect(readSalarySelection('?salary_min=3e8&salary_max=NaN'))
      .toEqual(DEFAULT_SALARY_SELECTION);
    expect(readSalarySelection('?salary_min=0&salary_max=100000000.00&salary_period=YEAR'))
      .toEqual({ ...DEFAULT_SALARY_SELECTION, min: '0', max: '100000000.00' });
  });
});
