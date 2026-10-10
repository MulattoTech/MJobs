import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  MatchWeightConfiguration,
  CANONICAL_DEFAULT_WEIGHTS,
} from './match-weight-configuration';

describe('MatchWeightConfiguration', () => {
  it('renders canonical specification weights and balanced status', () => {
    const html = renderToStaticMarkup(
      <MatchWeightConfiguration initialWeights={CANONICAL_DEFAULT_WEIGHTS} />
    );

    expect(html).toContain('Match Weight Configuration');
    expect(html).toContain('Total: 100% (Balanced)');
    expect(html).toContain('Balanced Default (Canonical Spec)');
    expect(html).toContain('title');
    expect(html).toContain('20%');
    expect(html).toContain('skills');
    expect(html).toContain('25%');
    expect(html).toContain('experience');
    expect(html).toContain('15%');
    expect(html).toContain('location');
    expect(html).toContain('salary');
    expect(html).toContain('10%');
    expect(html).toContain('education');
    expect(html).toContain('5%');
    expect(html).toContain('company');
    expect(html).toContain('industry');
    expect(html).toContain('Save Weights');
    expect(html).toContain('Reset to Canonical Default');
  });

  it('renders presets dropdown options', () => {
    const html = renderToStaticMarkup(
      <MatchWeightConfiguration initialWeights={CANONICAL_DEFAULT_WEIGHTS} />
    );

    expect(html).toContain('Technical Skills Focus');
    expect(html).toContain('Seniority &amp; Experience Focus');
    expect(html).toContain('Remote &amp; Compensation Focus');
    expect(html).toContain('Custom Weights');
  });

  it('renders unbalanced status when total deviates from 100%', () => {
    const customWeights = {
      ...CANONICAL_DEFAULT_WEIGHTS,
      skills: 40, // pushes total to 115%
    };
    const html = renderToStaticMarkup(
      <MatchWeightConfiguration initialWeights={customWeights} />
    );

    expect(html).toContain('Total: 115% (Unbalanced)');
    expect(html).toContain('Auto-Normalize to 100%');
  });
});
