import { describe, expect, it, vi } from 'vitest';
import {
  createRoleScoringProfileClient,
  RoleScoringProfileClientError,
  type RoleScoringProfileDraft,
} from './role-scoring-profile-client';

const profileId = 'profile 7';
const searchProfileId = 'search #2';
const draft: RoleScoringProfileDraft = {
  schema_version: 1,
  role_family: 'Cloud Operations',
  weights: { skills: 70, ai_intensity: 30 },
  thresholds: { skills: 60 },
};
const saved = {
  id: 'scoring-profile-1', version: 'version-2', schema_version: 1,
  role_family: 'Cloud Operations', weights: { skills: 70, ai_intensity: 30 },
  thresholds: { skills: 60 },
};
const previous = { ...saved, version: 'version-1', weights: { skills: 50 } };

function envelope(profile: unknown = saved, overrides: Record<string, unknown> = {}) {
  return { profile_id: profileId, search_profile_id: searchProfileId, profile, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

describe('saved-search role-scoring profile API client', () => {
  it('loads a typed profile using encoded selected profile and search identifiers', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(envelope()));
    const client = createRoleScoringProfileClient({ fetchImpl });

    const loaded = await client.load(profileId, searchProfileId);

    expect(loaded).toEqual(saved);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/profiles/profile%207/search-profiles/search%20%232/role-scoring-profile');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('saves with the exact expected version and reads back the same scoped profile', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse(envelope(previous)))
      .mockResolvedValueOnce(jsonResponse(envelope()))
      .mockResolvedValueOnce(jsonResponse(envelope()));
    const client = createRoleScoringProfileClient({ fetchImpl });

    const before = await client.load(profileId, searchProfileId);
    const written = await client.save(profileId, searchProfileId, draft, before!.version);
    const readback = await client.readback(profileId, searchProfileId);

    expect(before).toEqual(previous);
    expect(written).toEqual(saved);
    expect(readback).toEqual(saved);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    const [putUrl, putInit] = fetchImpl.mock.calls[1] as [string, RequestInit];
    expect(putUrl).toBe('/profiles/profile%207/search-profiles/search%20%232/role-scoring-profile');
    expect(putInit.method).toBe('PUT');
    expect(JSON.parse(String(putInit.body))).toEqual({
      expected_version: 'version-1', profile: draft,
    });
    expect((fetchImpl.mock.calls[2] as [string, RequestInit])[1].method).toBe('GET');
    for (const [url, init] of fetchImpl.mock.calls as [string, RequestInit][]) {
      expect(url).toContain('/role-scoring-profile');
      expect(url).not.toMatch(/submit|application|ats/i);
      expect(['GET', 'PUT']).toContain(init.method);
    }
  });

  it('normalizes a stale version conflict without leaking server details or suggesting retry', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      detail: { code: 'SCORING_PROFILE_VERSION_CONFLICT', secret: 'private-response-body' },
    }, 409));
    const client = createRoleScoringProfileClient({ fetchImpl });

    try {
      await client.save(profileId, searchProfileId, draft, 'stale-version');
      throw new Error('expected stale save to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(RoleScoringProfileClientError);
      expect(error).toMatchObject({
        kind: 'STALE_VERSION', status: 409, recovery: 'RELOAD', retryable: false,
      });
      expect((error as Error).message).toMatch(/reload/i);
      expect((error as Error).message).not.toMatch(/retry|private-response-body|SCORING_PROFILE/);
      expect(error).not.toHaveProperty('response');
      expect(error).not.toHaveProperty('body');
    }
  });

  it('rejects foreign profile or search echoes before exposing the saved profile', async () => {
    for (const overrides of [
      { profile_id: 'another-profile' },
      { search_profile_id: 'another-search' },
    ]) {
      const client = createRoleScoringProfileClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(envelope(saved, overrides))),
      });
      await expect(client.load(profileId, searchProfileId))
        .rejects.toMatchObject({ kind: 'SCOPE_MISMATCH', retryable: false, recovery: 'NONE' });
    }
  });

  it('rejects malformed profile envelopes without coercing field values', async () => {
    const malformed = [
      envelope({ ...saved, schema_version: 2 }),
      envelope({ ...saved, version: '' }),
      envelope({ ...saved, weights: { skills: Number.NaN } }),
      envelope({ ...saved, thresholds: { skills: true } }),
      { profile: saved, profile_id: profileId },
    ];
    for (const body of malformed) {
      const client = createRoleScoringProfileClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      await expect(client.load(profileId, searchProfileId))
        .rejects.toBeInstanceOf(RoleScoringProfileClientError);
    }
  });

  it('rejects path-changing IDs before making a request', async () => {
    const fetchImpl = vi.fn();
    const client = createRoleScoringProfileClient({ fetchImpl });

    await expect(client.load('profile/other', searchProfileId)).rejects.toMatchObject({
      kind: 'INVALID_REQUEST', retryable: false,
    });
    await expect(client.load(profileId, '..')).rejects.toMatchObject({
      kind: 'INVALID_REQUEST', retryable: false,
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
