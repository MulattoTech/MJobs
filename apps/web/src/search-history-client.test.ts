import { describe, expect, it, vi } from 'vitest';
import {
  createSearchHistoryClient,
  SearchHistoryClientError,
} from './search-history-client';

const record = {
  id: 'run-1',
  profile_id: 'profile-1',
  source: 'Greenhouse',
  query: 'Infrastructure Support Engineer',
  filters: { remote: true },
  started_at: '2026-10-08T15:00:00Z',
  finished_at: '2026-10-08T15:00:04Z',
  results: 12,
  new_jobs: 3,
  duplicates: 8,
  errors: ['A source timed out.'],
  status: 'FAILED',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('search history API client', () => {
  it('loads durable history with an encoded profile and maps API error strings to the panel shape', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      profile_id: 'profile 7', items: [{ ...record, profile_id: 'profile 7' }], next_cursor: 'older page',
    }));
    const client = createSearchHistoryClient({ baseUrl: 'http://127.0.0.1:8000/', fetchImpl });

    const result = await client.loadHistory('profile 7');

    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/profiles/profile%207/search-history?limit=50');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(result.profile_id).toBe('profile 7');
    expect(result.items[0]).toMatchObject({
      id: 'run-1', source: 'Greenhouse', query: 'Infrastructure Support Engineer',
      results: 12, new_jobs: 3, duplicates: 8, errors: 1, status: 'FAILED',
      error_detail: '1 recorded error.',
    });
    expect(result.next_cursor).toBe('older page');
  });

  it('encodes cursor pagination and accepts the documented limit range', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      profile_id: 'profile-1', items: [], next_cursor: null,
    }));
    const client = createSearchHistoryClient({ fetchImpl });

    await client.loadHistory('profile-1', { limit: 25, cursor: 'cursor +/=' });

    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    const parsed = new URL(url, 'http://local.test');
    expect(parsed.pathname).toBe('/profiles/profile-1/search-history');
    expect(parsed.searchParams.get('limit')).toBe('25');
    expect(parsed.searchParams.get('cursor')).toBe('cursor +/=');
  });

  it('preserves null metrics rather than coercing unmeasured results to zero', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      profile_id: 'profile-1',
      items: [{ ...record, results: null, new_jobs: null, duplicates: null }],
      next_cursor: null,
    }));
    const client = createSearchHistoryClient({ fetchImpl });

    const result = await client.loadHistory('profile-1');

    expect(result.items[0].results).toBeNull();
    expect(result.items[0].new_jobs).toBeNull();
    expect(result.items[0].duplicates).toBeNull();
  });

  it('rejects invalid page sizes and cursors before making a request', async () => {
    const fetchImpl = vi.fn();
    const client = createSearchHistoryClient({ fetchImpl });

    for (const options of [
      { limit: 0 }, { limit: 101 }, { limit: 1.5 }, { cursor: '' }, { cursor: 'x'.repeat(1025) },
    ]) {
      await expect(client.loadHistory('profile-1', options))
        .rejects.toMatchObject({ kind: 'INVALID_REQUEST', retryable: false });
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('rejects foreign-profile, invalid-pagination, and malformed record responses', async () => {
    const invalidBodies = [
      { profile_id: 'profile-2', items: [{ ...record, profile_id: 'profile-2' }], next_cursor: null },
      { profile_id: 'profile-1', items: [{ ...record, profile_id: 'profile-2' }], next_cursor: null },
      { profile_id: 'profile-1', items: [record], next_cursor: '' },
      { profile_id: 'profile-1', items: [{ ...record, errors: 'not-an-array' }], next_cursor: null },
    ];

    for (const body of invalidBodies) {
      const client = createSearchHistoryClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      await expect(client.loadHistory('profile-1'))
        .rejects.toBeInstanceOf(SearchHistoryClientError);
    }
  });

  it('maps HTTP errors without reading or exposing raw server error bodies', async () => {
    const rawBodyReader = vi.fn().mockRejectedValue(new Error('server token=secret'));
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false, status: 503, json: rawBodyReader,
    } as unknown as Response);
    const client = createSearchHistoryClient({ fetchImpl });

    try {
      await client.loadHistory('profile-1');
      throw new Error('expected HTTP error');
    } catch (error) {
      expect(error).toBeInstanceOf(SearchHistoryClientError);
      expect(error).toMatchObject({ kind: 'HTTP_ERROR', status: 503, retryable: true });
      expect((error as Error).message).not.toContain('server token=secret');
    }
    expect(rawBodyReader).not.toHaveBeenCalled();
  });

  it('sanitizes network failures and bounded per-run error details', async () => {
    const fetchImpl = vi.fn()
      .mockRejectedValueOnce(new Error('offline token=private'))
      .mockResolvedValueOnce(jsonResponse({
        profile_id: 'profile-1',
        items: [{ ...record, errors: ['token=private\nconnection reset'] }],
        next_cursor: null,
      }));
    const client = createSearchHistoryClient({ fetchImpl });

    try {
      await client.loadHistory('profile-1');
      throw new Error('expected network error');
    } catch (error) {
      expect(error).toMatchObject({ kind: 'NETWORK', status: null, retryable: true });
      expect((error as Error).message).not.toContain('offline token=private');
    }
    const loaded = await client.loadHistory('profile-1');
    expect(loaded.items[0].errors).toBe(1);
    expect(loaded.items[0].error_detail).toBe('1 recorded error.');
    expect(loaded.items[0].error_detail).not.toContain('token=private');
  });
});
