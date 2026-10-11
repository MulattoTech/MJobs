import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { FreshJobPriorityBadge } from './fresh-job-priority-badge';

describe('FreshJobPriorityBadge', () => {
  it('renders critical expedited tier correctly in full view', () => {
    const html = renderToStaticMarkup(
      <FreshJobPriorityBadge
        priorityTier="CRITICAL"
        freshnessBand="FRESH"
        matchScore={92}
        matchQuality="EXCELLENT"
        ageHours={4.5}
        explanation="New excellent match processed first for expedited review."
      />
    );

    expect(html).toContain('QUEUE DISPATCH PRIORITY');
    expect(html).toContain('Expedited • New Excellent Match');
    expect(html).toContain('Tier: CRITICAL');
    expect(html).toContain('92% (EXCELLENT)');
    expect(html).toContain('FRESH (4.5h old)');
    expect(html).toContain('New excellent matches are processed ahead of stale lower-quality jobs');
  });

  it('renders compact badge for card view', () => {
    const html = renderToStaticMarkup(
      <FreshJobPriorityBadge
        priorityTier="HIGH"
        compact={true}
      />
    );

    expect(html).toContain('tag badge-tier-high');
    expect(html).toContain('High Priority • Fresh Match');
  });

  it('renders deferred tier for aging jobs', () => {
    const html = renderToStaticMarkup(
      <FreshJobPriorityBadge
        priorityTier="DEFERRED"
        freshnessBand="AGING"
        ageHours={240}
        matchScore={75}
      />
    );

    expect(html).toContain('Deferred • Aging Posting');
    expect(html).toContain('AGING (10.0d old)');
    expect(html).toContain('Tier: DEFERRED');
  });

  it('renders nothing when priority tier is missing or invalid', () => {
    const html1 = renderToStaticMarkup(<FreshJobPriorityBadge priorityTier={null} />);
    expect(html1).toBe('');

    const html2 = renderToStaticMarkup(<FreshJobPriorityBadge priorityTier={undefined} />);
    expect(html2).toBe('');
  });
});
