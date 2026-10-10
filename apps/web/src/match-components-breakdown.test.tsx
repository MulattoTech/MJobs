import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  MatchComponentsBreakdown,
  type DimensionContribution,
} from './match-components-breakdown';

describe('MatchComponentsBreakdown', () => {
  const sampleDimensions: DimensionContribution[] = [
    {
      dimension: 'title_similarity',
      label: 'Title Similarity',
      weight: 15.0,
      raw_score: 95.0,
      weighted_contribution: 14.25,
      status: 'KNOWN',
      explanation: "Title matches 'Senior Platform Engineer' (95.0%).",
      provenance: { source: 'APPROVED_TITLE_OVERLAP' },
    },
    {
      dimension: 'required_skills',
      label: 'Required Skills',
      weight: 20.0,
      raw_score: 100.0,
      weighted_contribution: 20.0,
      status: 'KNOWN',
      explanation: 'Candidate possesses all 2 required skills.',
      provenance: { matched_skills: ['Python', 'PostgreSQL'] },
    },
    {
      dimension: 'certifications',
      label: 'Certifications',
      weight: 5.0,
      raw_score: null,
      weighted_contribution: null,
      status: 'UNKNOWN',
      explanation: 'Certifications are UNKNOWN: posting lists no certifications.',
      provenance: { source: 'UNKNOWN' },
    },
  ];

  it('renders overall match score and coverage percentage', () => {
    const html = renderToStaticMarkup(
      <MatchComponentsBreakdown
        score={88.5}
        coveragePercent={91.7}
        dimensions={sampleDimensions}
        knownDimensions={['title_similarity', 'required_skills']}
        unknownDimensions={['certifications']}
        advisory={true}
      />
    );

    expect(html).toContain('12-Dimension Match Evaluation');
    expect(html).toContain('Match Score: 88.5%');
    expect(html).toContain('Advisory Match');
    expect(html).toContain('92%'); // Coverage percent display rounded
    expect(html).toContain('2 known, 1 unstated/unknown');
    expect(html).toContain('Title Similarity');
    expect(html).toContain('+14.3 pts');
    expect(html).toContain('Required Skills');
    expect(html).toContain('+20.0 pts');
    expect(html).toContain('Certifications');
    expect(html).toContain('UNKNOWN');
  });

  it('renders negative requirements alert when dealbreaker signals triggered', () => {
    const html = renderToStaticMarkup(
      <MatchComponentsBreakdown
        score={35.0}
        coveragePercent={85.0}
        dimensions={sampleDimensions}
        knownDimensions={['title_similarity', 'required_skills']}
        unknownDimensions={['certifications']}
        negativeSignalsTriggered={['polygraph', 'crypto']}
        advisory={true}
      />
    );

    expect(html).toContain('Negative Requirement Alert:');
    expect(html).toContain('polygraph, crypto');
    expect(html).toContain('A penalty has been applied to this match.');
  });

  it('handles empty dimensions gracefully', () => {
    const html = renderToStaticMarkup(
      <MatchComponentsBreakdown
        score={null}
        coveragePercent={0}
        dimensions={[]}
        knownDimensions={[]}
        unknownDimensions={[]}
        advisory={false}
      />
    );

    expect(html).toContain('12-Dimension Match Evaluation');
    expect(html).toContain('Match Score: Incomplete');
    expect(html).toContain('Signal Coverage:');
    expect(html).toContain('0 known, 0 unstated/unknown');
  });
});
