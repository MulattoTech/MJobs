import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  QualificationStrengthBreakdown,
  type QualificationItem,
} from './qualification-strength-breakdown';

describe('QualificationStrengthBreakdown', () => {
  const sampleItems: QualificationItem[] = [
    {
      name: 'Python',
      qualification_type: 'SKILL',
      strength: 'REQUIRED',
      weight: 1.0,
      candidate_status: 'COVERED',
      score_impact: 'Mandatory qualification satisfied (+1.0 weight).',
    },
    {
      name: 'PostgreSQL',
      qualification_type: 'SKILL',
      strength: 'REQUIRED',
      weight: 1.0,
      candidate_status: 'MISSING',
      score_impact: 'Missing mandatory qualification — core requirement not met (-1.0 weight impact).',
    },
    {
      name: 'Kubernetes',
      qualification_type: 'SKILL',
      strength: 'PREFERRED',
      weight: 0.5,
      candidate_status: 'COVERED',
      score_impact: 'Preferred qualification satisfied (+0.5 bonus weight).',
    },
    {
      name: 'Terraform',
      qualification_type: 'SKILL',
      strength: 'PREFERRED',
      weight: 0.5,
      candidate_status: 'MISSING',
      score_impact: 'Missing preferred qualification — desirable but not disqualifying (capped at -0.5 weight impact).',
    },
    {
      name: 'GraphQL',
      qualification_type: 'SKILL',
      strength: 'NICE_TO_HAVE',
      weight: 0.25,
      candidate_status: 'COVERED',
      score_impact: 'Nice-to-have qualification satisfied (+0.25 bonus weight).',
    },
    {
      name: 'Redis',
      qualification_type: 'SKILL',
      strength: 'UNKNOWN',
      weight: 0.25,
      candidate_status: 'MISSING',
      score_impact: 'Uncovered unknown skill (0.25 weight impact).',
    },
  ];

  it('renders all 4 requirement strengths: REQUIRED, PREFERRED, NICE_TO_HAVE, and UNKNOWN', () => {
    const html = renderToStaticMarkup(
      <QualificationStrengthBreakdown
        items={sampleItems}
        requiredCoveragePercent={50}
        preferredCoveragePercent={50}
        overallStrengthScore={62.5}
        counts={{
          REQUIRED: { total: 2, covered: 1, missing: 1, coverage_ratio: 0.5 },
          PREFERRED: { total: 2, covered: 1, missing: 1, coverage_ratio: 0.5 },
          NICE_TO_HAVE: { total: 1, covered: 1, missing: 0, coverage_ratio: 1.0 },
          UNKNOWN: { total: 1, covered: 0, missing: 1, coverage_ratio: 0.0 },
        }}
        explanation="Qualification strength analysis: 1/2 required met (50.0%); 1/2 preferred met (50.0%)."
      />
    );

    expect(html).toContain('Required vs Preferred Qualifications');
    expect(html).toContain('REQUIRED');
    expect(html).toContain('PREFERRED');
    expect(html).toContain('NICE_TO_HAVE');
    expect(html).toContain('UNKNOWN');

    // KPI cards
    expect(html).toContain('MANDATORY REQUIREMENTS');
    expect(html).toContain('50% Met');
    expect(html).toContain('1/2 verified');
    expect(html).toContain('PREFERRED QUALIFICATIONS');
    expect(html).toContain('Alignment: 63%');

    // Score impact descriptions showing non-severe preferred penalty
    expect(html).toContain('Missing mandatory qualification — core requirement not met (-1.0 weight impact).');
    expect(html).toContain('Missing preferred qualification — desirable but not disqualifying');

    // Filter tab buttons
    expect(html).toContain('All (6)');
    expect(html).toContain('Required (2)');
    expect(html).toContain('Preferred (2)');
  });

  it('handles empty qualification list gracefully', () => {
    const html = renderToStaticMarkup(
      <QualificationStrengthBreakdown items={[]} />
    );

    expect(html).toContain('Required vs Preferred Qualifications');
    expect(html).toContain('No qualifications match the selected filter.');
  });
});
