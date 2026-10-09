import { afterEach, describe, expect, it, vi } from 'vitest';
import { createCompanyPreferenceClient } from './company-preferences-client';

afterEach(() => vi.unstubAllGlobals());

describe('company preference API client', () => {
  it('uses profile and company scoped routes and sends only an explicit state', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      profile_id: 'profile/one', company_id: 'company one', status: 'PREFERRED',
      updated_at: '2026-10-09T09:00:00Z',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('crypto', { randomUUID: () => 'request-1' });
    const client = createCompanyPreferenceClient();

    const saved = await client.set('profile/one', 'company one', 'PREFERRED');
    expect(saved.status).toBe('PREFERRED');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      '/profiles/profile%2Fone/company-preferences/company%20one',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ status: 'PREFERRED', request_id: 'request-1' }),
      }),
    );
  });

  it('reuses the request id after a transport failure so save retry is idempotent', async () => {
    let nextId = 0;
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockImplementation(async () => new Response(JSON.stringify({
        profile_id: 'profile', company_id: 'company', status: 'BLACKLISTED',
        updated_at: '2026-10-09T09:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('crypto', { randomUUID: () => `request-${++nextId}` });
    const client = createCompanyPreferenceClient();

    await expect(client.set('profile', 'company', 'BLACKLISTED')).rejects.toThrow('offline');
    await client.set('profile', 'company', 'BLACKLISTED');
    const firstBody = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    const retryBody = JSON.parse(fetchMock.mock.calls[1][1]?.body as string);
    expect(retryBody).toEqual(firstBody);
    expect(nextId).toBe(1);
  });
});
