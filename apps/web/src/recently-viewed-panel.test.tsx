import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it} from 'vitest';
import {RecentlyViewedPanelView} from './recently-viewed-panel';

const response = {
  profile_id: 'profile-1', max_items: 100, retention_days: 90,
  items: [
    {job_id: 'job-1', title: 'Network Engineer', company: 'Example Inc.',
     viewed_at: '2026-11-04T06:10:00+00:00', available: true},
    {job_id: 'removed', title: null, company: null,
     viewed_at: '2026-11-03T06:10:00+00:00', available: false},
  ],
};

const callbacks = {onRefresh: () => {}, onClear: () => {}, onOpenJob: () => {}};

describe('recently viewed panel', () => {
  it('shows visits separately from saves and applications, with clear/refresh controls', () => {
    const html = renderToStaticMarkup(<RecentlyViewedPanelView
      profileId="profile-1" state={{kind:'ready',response}}
      clearing={false} clearError={false} {...callbacks}/>);
    expect(html).toContain('Recently viewed');
    expect(html).toContain('A visit is not a save or an application');
    expect(html).toContain('Saved-only filtering is unavailable');
    expect(html).toContain('Network Engineer');
    expect(html).toContain('Example Inc.');
    expect(html).toContain('Job no longer in the local catalog');
    expect(html).toContain('Clear visit history');
    expect(html).toContain('last 90 days');
    expect((html.match(/<button/g) || []).length).toBe(3);
  });

  it('hides another profile response and provides loading and error states', () => {
    const mismatch = renderToStaticMarkup(<RecentlyViewedPanelView
      profileId="other" state={{kind:'ready',response}}
      clearing={false} clearError={false} {...callbacks}/>);
    expect(mismatch).not.toContain('Network Engineer');
    expect(mismatch).toContain('Loading job visits');
    const error = renderToStaticMarkup(<RecentlyViewedPanelView
      profileId="profile-1" state={{kind:'error'}}
      clearing={false} clearError {...callbacks}/>);
    expect(error).toContain('Could not load recent visits');
    expect(error).toContain('Could not clear visit history');
  });

  it('makes no visit list when a profile is not selected', () => {
    const html = renderToStaticMarkup(<RecentlyViewedPanelView
      profileId="" state={{kind:'loading'}}
      clearing={false} clearError={false} {...callbacks}/>);
    expect(html).toContain('Select a profile');
    expect(html).not.toContain('Network Engineer');
  });
});
