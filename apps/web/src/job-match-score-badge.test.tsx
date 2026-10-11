import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { JobMatchScoreBadge } from './job-match-score-badge';

describe('JobMatchScoreBadge', () => {
  it('renders canonical requirement example: 91/100 — Excellent Match in full card', () => {
    const html = renderToStaticMarkup(
      <JobMatchScoreBadge
        score={91}
        scoreFormatted="91/100 — Excellent Match"
        category="EXCELLENT"
        categoryLabel="Excellent Match"
        coveragePercent={92}
        recommendation="High alignment: recommended for prioritized application preparation."
        advisory={true}
      />
    );

    expect(html).toContain('JOB MATCH TRANSPARENCY (MJ-REQ-112)');
    expect(html).toContain('91/100 — Excellent Match');
    expect(html).toContain('Category: EXCELLENT');
    expect(html).toContain('Advisory Match');
    expect(html).toContain('High alignment: recommended for prioritized application preparation.');
    expect(html).toContain('Signal Coverage: 92%');
  });

  it('renders compact badge for job card listing', () => {
    const html = renderToStaticMarkup(
      <JobMatchScoreBadge
        score={91}
        scoreFormatted="91/100 — Excellent Match"
        category="EXCELLENT"
        compact={true}
      />
    );

    expect(html).toContain('tag job-match-badge-compact');
    expect(html).toContain('91/100 — Excellent Match');
    expect(html).toContain('⭐');
  });

  it('renders exceptional match with 🌟 icon', () => {
    const html = renderToStaticMarkup(
      <JobMatchScoreBadge
        score={98.5}
        scoreFormatted="98.5/100 — Exceptional Match"
        category="EXCEPTIONAL"
        categoryLabel="Exceptional Match"
        coveragePercent={98}
      />
    );

    expect(html).toContain('🌟');
    expect(html).toContain('98.5/100 — Exceptional Match');
    expect(html).toContain('Category: EXCEPTIONAL');
  });

  it('renders dealbreaker alert banner when negative signals exist', () => {
    const html = renderToStaticMarkup(
      <JobMatchScoreBadge
        score={35}
        scoreFormatted="35/100 — Poor Match"
        category="POOR"
        categoryLabel="Poor Match"
        negativeSignalsTriggered={['crypto', 'gambling']}
        recommendation="Caution: Triggers dealbreaker avoidance term(s): crypto, gambling."
      />
    );

    expect(html).toContain('Dealbreaker Alert:');
    expect(html).toContain('crypto, gambling');
    expect(html).toContain('Caution: Triggers dealbreaker avoidance term(s)');
  });

  it('handles missing score gracefully', () => {
    const html = renderToStaticMarkup(
      <JobMatchScoreBadge
        score={null}
        category="INCOMPLETE"
      />
    );

    expect(html).toContain('Incomplete — Insufficient Data');
    expect(html).toContain('Category: INCOMPLETE');
  });
});
