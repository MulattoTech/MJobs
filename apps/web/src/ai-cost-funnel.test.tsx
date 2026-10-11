import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AICostFunnelView,
  DEFAULT_COST_POLICY,
  validateCostPolicy,
  type AICostPolicyConfig,
} from './ai-cost-funnel';

describe('validateCostPolicy', () => {
  it('accepts valid default policy', () => {
    expect(validateCostPolicy(DEFAULT_COST_POLICY)).toBeNull();
  });

  it('rejects minimum score out of 0-100 range', () => {
    expect(validateCostPolicy({ ...DEFAULT_COST_POLICY, minimumScore: -1 })).toBe(
      'Minimum score threshold must be an integer between 0 and 100.',
    );
    expect(validateCostPolicy({ ...DEFAULT_COST_POLICY, minimumScore: 101 })).toBe(
      'Minimum score threshold must be an integer between 0 and 100.',
    );
  });

  it('rejects input candidate ceiling less than 1 or over 100k', () => {
    expect(validateCostPolicy({ ...DEFAULT_COST_POLICY, maxInputCandidates: 0 })).toBe(
      'Maximum input candidates must be between 1 and 100,000.',
    );
    expect(validateCostPolicy({ ...DEFAULT_COST_POLICY, maxInputCandidates: 100001 })).toBe(
      'Maximum input candidates must be between 1 and 100,000.',
    );
  });

  it('rejects document candidates exceeding AI candidates ceiling', () => {
    expect(
      validateCostPolicy({
        ...DEFAULT_COST_POLICY,
        maxAiCandidates: 50,
        maxDocumentCandidates: 60,
      }),
    ).toBe('Maximum document candidates cannot exceed the maximum AI candidate ceiling.');
  });
});

describe('AICostFunnelView Component', () => {
  const sampleMetrics = {
    receivedCount: 5000,
    uniqueCount: 4200,
    plausibleCount: 800,
    eligibleCount: 200,
    aiCandidateCount: 75,
    documentCandidateCount: 20,
  };

  it('renders all funnel stages and titles', () => {
    const html = renderToStaticMarkup(
      <AICostFunnelView policy={DEFAULT_COST_POLICY} currentMetrics={sampleMetrics} />,
    );

    expect(html).toContain('AI Candidate Cost Funnel');
    expect(html).toContain('1. Inbound Discovery');
    expect(html).toContain('2. Deterministic Deduplication');
    expect(html).toContain('3. Plausible Pre-Filters');
    expect(html).toContain('4. Deterministic Score Gate');
    expect(html).toContain('5. AI Candidate Evaluation');
    expect(html).toContain('6. Review-Gated Documents');
  });

  it('displays candidate metrics numbers for each stage', () => {
    const html = renderToStaticMarkup(
      <AICostFunnelView policy={DEFAULT_COST_POLICY} currentMetrics={sampleMetrics} />,
    );

    expect(html).toContain('5,000');
    expect(html).toContain('4,200');
    expect(html).toContain('800');
    expect(html).toContain('200');
    expect(html).toContain('75');
    expect(html).toContain('20');
  });

  it('displays threshold and cap badges', () => {
    const customPolicy: AICostPolicyConfig = {
      minimumScore: 85,
      maxInputCandidates: 3000,
      maxAiCandidates: 50,
      maxDocumentCandidates: 10,
    };
    const html = renderToStaticMarkup(<AICostFunnelView policy={customPolicy} />);

    expect(html).toContain('Threshold:');
    expect(html).toContain('85');
    expect(html).toContain('Cap:');
    expect(html).toContain('3,000');
    expect(html).toContain('50');
    expect(html).toContain('10');
  });

  it('renders threshold input controls and buttons', () => {
    const html = renderToStaticMarkup(<AICostFunnelView policy={DEFAULT_COST_POLICY} />);

    expect(html).toContain('min-score-input');
    expect(html).toContain('max-input-candidates');
    expect(html).toContain('max-ai-candidates');
    expect(html).toContain('max-doc-candidates');
    expect(html).toContain('save-funnel-policy-btn');
    expect(html).toContain('reset-funnel-policy-btn');
  });
});
