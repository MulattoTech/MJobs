import {describe, expect, it, vi} from 'vitest';
import {createRecentlyViewedClient} from './recently-viewed-client';

const row = {
  job_id: 'job-1', title: 'Network Engineer', company: 'Example Inc.',
  viewed_at: '2026-11-04T06:10:00+00:00', available: true,
};
const result = {profile_id: 'profile-1', items: [row], max_items: 100, retention_days: 90};

describe('recently viewed API client', () => {
  it('uses explicit profile/job paths and never calls application endpoints', async () => {
    const request = vi.fn().mockResolvedValueOnce(result)
      .mockResolvedValueOnce({profile_id: 'profile-1', job_id: 'job-1', viewed_at: row.viewed_at})
      .mockResolvedValueOnce({profile_id: 'profile-1', cleared: 1});
    const client = createRecentlyViewedClient(request);
    expect(await client.list('profile-1')).toEqual(result);
    await client.record('profile-1', 'job-1');
    await client.clear('profile-1');
    expect(request.mock.calls.map(call => call[0])).toEqual([
      '/profiles/profile-1/jobs/recently-viewed?limit=100',
      '/profiles/profile-1/jobs/job-1/view',
      '/profiles/profile-1/jobs/recently-viewed',
    ]);
    expect(request.mock.calls[1][1]).toEqual({method: 'POST'});
    expect(request.mock.calls[2][1]).toEqual({method: 'DELETE'});
  });

  it('rejects another profile, duplicate rows, malformed dates and invalid identifiers', async () => {
    const wrongProfile = createRecentlyViewedClient(vi.fn().mockResolvedValue({...result, profile_id: 'other'}));
    await expect(wrongProfile.list('profile-1')).rejects.toThrow(/profile mismatch/);
    const duplicate = createRecentlyViewedClient(vi.fn().mockResolvedValue({...result, items: [row, row]}));
    await expect(duplicate.list('profile-1')).rejects.toThrow(/Invalid view history row/);
    const badDate = createRecentlyViewedClient(vi.fn().mockResolvedValue({
      ...result, items: [{...row, viewed_at: 'invalid'}],
    }));
    await expect(badDate.list('profile-1')).rejects.toThrow(/Invalid view history row/);
    await expect(wrongProfile.record('../other', 'job-1')).rejects.toThrow(/Invalid profile/);
  });

  it('does not accept ambiguous write receipts after a timeout or failed request', async () => {
    const wrongReceipt = createRecentlyViewedClient(vi.fn().mockResolvedValue({
      profile_id: 'other', job_id: 'job-1', viewed_at: row.viewed_at,
    }));
    await expect(wrongReceipt.record('profile-1', 'job-1')).rejects.toThrow(/receipt mismatch/);
    const wrongClear = createRecentlyViewedClient(vi.fn().mockResolvedValue({
      profile_id: 'profile-1', cleared: 101,
    }));
    await expect(wrongClear.clear('profile-1')).rejects.toThrow(/receipt mismatch/);
    const failed = createRecentlyViewedClient(vi.fn().mockRejectedValue(new Error('timeout')));
    await expect(failed.record('profile-1', 'job-1')).rejects.toThrow('timeout');
  });
});
