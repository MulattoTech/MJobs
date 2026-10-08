import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { SourceCandidate } from './source-candidate-review';
import type { SourceCandidateReviewClient, SourceCandidateReviewInput } from './source-candidate-review-client';
import {
  filterAndSortSourceCandidates,
  SourceCandidateReviewPanel,
  SourceCandidateReviewPanelController,
} from './source-candidate-review-panel';

const candidate = (profileId: string, id = `${profileId}-candidate`): SourceCandidate => ({
  id, profile_id: profileId, domain: `${profileId}.example`, display_name: profileId,
  site_type: 'UNKNOWN', state: 'NEEDS_REVIEW', first_seen_at: '2026-10-08T10:00:00Z',
  last_seen_at: '2026-10-08T10:30:00Z', collection_enabled: false, review_reason: null,
  integration_issue: null, integration_issue_status: 'NOT_LINKED', observations: [], review_history: [],
});

const input: SourceCandidateReviewInput = {
  state: 'NEEDS_REVIEW', site_type: 'ATS', reason: 'Destination only.',
  api_feed_evidence_status: 'UNKNOWN', api_feed_evidence_reference: null,
  api_feed_evidence_note: 'Feed unknown.', access_terms_note: 'Terms unknown.',
  capabilities_limits_note: 'Capabilities unknown.', expected_value_maintenance_note: 'Value unknown.',
  integration_issue: null, request_id: 'stable-review-request',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe('source candidate review panel controller', () => {
  it('clears profile-scoped rows and ignores an older delayed response after selection changes', async () => {
    const oldRequest = deferred<SourceCandidate[]>();
    const newRequest = deferred<SourceCandidate[]>();
    const client: SourceCandidateReviewClient = {
      list: vi.fn((profileId: string) => profileId === 'old' ? oldRequest.promise : newRequest.promise),
      review: vi.fn(),
    };
    const controller = new SourceCandidateReviewPanelController(client);

    const oldLoad = controller.selectProfile('old');
    expect(controller.getSnapshot()).toMatchObject({ profileId: 'old', candidates: [], loading: true });
    const newLoad = controller.selectProfile('new');
    expect(controller.getSnapshot()).toMatchObject({ profileId: 'new', candidates: [], loading: true, error: null });
    newRequest.resolve([candidate('new')]);
    await newLoad;
    oldRequest.reject(new Error('stale old profile failure'));
    await oldLoad;

    expect(controller.getSnapshot()).toMatchObject({ profileId: 'new', candidates: [candidate('new')], loading: false, error: null });
    expect(JSON.stringify(controller.getSnapshot())).not.toContain('stale old profile');
  });

  it('retains review request identity on recoverable errors and only reloads after review success', async () => {
    const saved = candidate('profile-1');
    const list = vi.fn().mockResolvedValue([saved]);
    const review = vi.fn().mockRejectedValueOnce(new Error('Temporary review error.'))
      .mockResolvedValueOnce(saved);
    const controller = new SourceCandidateReviewPanelController({ list, review });
    await controller.selectProfile('profile-1');
    const selected = controller.getSnapshot().candidates[0];

    await expect(controller.review('profile-1', selected.id, input)).rejects.toThrow('Temporary review error.');
    expect(list).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().candidates).toEqual([saved]);

    await controller.review('profile-1', selected.id, input);
    expect(review).toHaveBeenCalledTimes(2);
    expect(review.mock.calls[0]).toEqual(review.mock.calls[1]);
    expect(review.mock.calls[1][2].request_id).toBe('stable-review-request');
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('does not display a successful old-profile write or start readback after switching profiles', async () => {
    const oldReview = deferred<SourceCandidate>();
    const oldList = deferred<SourceCandidate[]>();
    const client: SourceCandidateReviewClient = {
      list: vi.fn((profileId: string) => profileId === 'old' ? oldList.promise : Promise.resolve([candidate('new')])),
      review: vi.fn(() => oldReview.promise),
    };
    const controller = new SourceCandidateReviewPanelController(client);
    const oldLoad = controller.selectProfile('old');
    oldList.resolve([candidate('old')]);
    await oldLoad;
    const reviewRequest = controller.review('old', 'old-candidate', input);
    await controller.selectProfile('new');
    oldReview.resolve(candidate('old'));
    await reviewRequest;

    expect(controller.getSnapshot()).toMatchObject({ profileId: 'new', candidates: [candidate('new')] });
    expect(client.list).toHaveBeenCalledTimes(2);
    expect(client.review).toHaveBeenCalledWith('old', 'old-candidate', input);
  });

  it('fails closed on a mismatched review response without refreshing the selected profile', async () => {
    const list = vi.fn().mockResolvedValue([candidate('profile-1')]);
    const client: SourceCandidateReviewClient = {
      list,
      review: vi.fn().mockResolvedValue(candidate('other-profile', 'candidate-1')),
    };
    const controller = new SourceCandidateReviewPanelController(client);
    await controller.selectProfile('profile-1');

    await expect(controller.review('profile-1', 'candidate-1', input)).rejects.toThrow(/response.*profile/i);
    expect(list).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().candidates).toEqual([candidate('profile-1')]);
  });

  it('rereads only on refresh-token changes and preserves the active filters', async () => {
    const list = vi.fn().mockResolvedValue([candidate('profile-1')]);
    const controller = new SourceCandidateReviewPanelController({ list, review: vi.fn() });

    await controller.syncSelection('profile-1', 0);
    controller.setFilters('BLOCKED', 'talenthop');
    await controller.syncSelection('profile-1', 0);
    expect(list).toHaveBeenCalledTimes(1);

    list.mockResolvedValueOnce([candidate('profile-1', 'after-capture')]);
    await controller.syncSelection('profile-1', 1);

    expect(list).toHaveBeenCalledTimes(2);
    expect(list).toHaveBeenLastCalledWith('profile-1');
    expect(controller.getSnapshot()).toMatchObject({
      profileId: 'profile-1', candidates: [candidate('profile-1', 'after-capture')],
      stateFilter: 'BLOCKED', sourceQuery: 'talenthop', loading: false,
    });

    list.mockResolvedValueOnce([candidate('profile-2')]);
    await controller.syncSelection('profile-2', 1);
    expect(list).toHaveBeenCalledTimes(3);
    expect(list).toHaveBeenLastCalledWith('profile-2');
    expect(controller.getSnapshot()).toMatchObject({
      profileId: 'profile-2', stateFilter: 'ALL', sourceQuery: '',
    });
  });
});

describe('source candidate review panel filters', () => {
  it('filters by review state and source name or domain, then orders deterministically', () => {
    const rows = [
      { ...candidate('profile-1', 'z-id'), state: 'NEEDS_REVIEW' as const, domain: 'zeta.example', display_name: 'Zeta' },
      { ...candidate('profile-1', 'b-id'), state: 'BLOCKED' as const, domain: 'beta.example', display_name: 'Beta' },
      { ...candidate('profile-1', 'a-id'), state: 'NEEDS_REVIEW' as const, domain: 'alpha.example', display_name: 'Alpha' },
      { ...candidate('other-profile', 'other-id'), state: 'NEEDS_REVIEW' as const, domain: 'alpha.example', display_name: 'Alpha' },
    ];

    expect(filterAndSortSourceCandidates(rows, 'profile-1', 'NEEDS_REVIEW').map(row => row.id))
      .toEqual(['a-id', 'z-id']);
    expect(filterAndSortSourceCandidates(rows, 'profile-1', 'ALL', 'ZETA').map(row => row.id))
      .toEqual(['z-id']);
    expect(filterAndSortSourceCandidates(rows, 'profile-1', 'ALL', 'beta.example').map(row => row.id))
      .toEqual(['b-id']);
  });

  it('exposes accessible read-only filters and keeps review as the only write action', () => {
    const html = renderToStaticMarkup(<SourceCandidateReviewPanel
      profileId="profile-1"
      client={{ list: vi.fn().mockResolvedValue([]), review: vi.fn() }}
    />);

    expect(html).toContain('aria-label="Filter by review state"');
    expect(html).toContain('aria-label="Filter by source name or domain"');
    expect(html).toContain('Sorted by review priority, then source domain');
    expect(html).toContain('Loading source candidates');
    expect(html).not.toContain('Enable collection');
    expect(html).not.toContain('Start collection');
  });
});
