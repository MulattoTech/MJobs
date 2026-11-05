import { api } from './api';

export type RecentJob = {
  job_id: string;
  title: string | null;
  company: string | null;
  viewed_at: string;
  available: boolean;
};

export type RecentlyViewedResponse = {
  profile_id: string;
  items: RecentJob[];
  max_items: number;
  retention_days: number;
};

type Request = <T>(path: string, init?: RequestInit) => Promise<T>;

function identifier(value: string): string {
  if (typeof value !== 'string' || !value.trim() || value.trim() !== value ||
      value.length > 128 || value === '.' || value === '..' ||
      /[\\/\u0000-\u001f\u007f]/.test(value)) {
    throw new Error('Invalid profile or job identifier.');
  }
  return encodeURIComponent(value);
}

function normalize(value: unknown, profileId: string): RecentlyViewedResponse {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid view history response.');
  const result = value as Record<string, unknown>;
  if (result.profile_id !== profileId) throw new Error('View history profile mismatch.');
  if (!Array.isArray(result.items) || result.items.length > 100 ||
      result.max_items !== 100 || result.retention_days !== 90) {
    throw new Error('Invalid view history response.');
  }
  const seen = new Set<string>();
  const items: RecentJob[] = [];
  for (const row of result.items) {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('Invalid view history row.');
    const item = row as Record<string, unknown>;
    if (typeof item.job_id !== 'string' || !item.job_id || item.job_id.length > 128 ||
        seen.has(item.job_id) || typeof item.viewed_at !== 'string' ||
        !Number.isFinite(Date.parse(item.viewed_at)) || typeof item.available !== 'boolean' ||
        !(item.title === null || (typeof item.title === 'string' && item.title.length <= 500)) ||
        !(item.company === null || (typeof item.company === 'string' && item.company.length <= 200)) ||
        (item.available && (!item.title || !item.company)) ||
        (!item.available && (item.title !== null || item.company !== null))) {
      throw new Error('Invalid view history row.');
    }
    seen.add(item.job_id);
    items.push(item as RecentJob);
  }
  return { profile_id: profileId, items, max_items: 100, retention_days: 90 };
}

export function createRecentlyViewedClient(request: Request = api) {
  return {
    async list(profileId: string): Promise<RecentlyViewedResponse> {
      const value = await request<unknown>(`/profiles/${identifier(profileId)}/jobs/recently-viewed?limit=100`);
      return normalize(value, profileId);
    },
    async record(profileId: string, jobId: string): Promise<void> {
      const response = await request<{profile_id: string; job_id: string; viewed_at: string}>(
        `/profiles/${identifier(profileId)}/jobs/${identifier(jobId)}/view`, { method: 'POST' },
      );
      if (response.profile_id !== profileId || response.job_id !== jobId ||
          !Number.isFinite(Date.parse(response.viewed_at))) throw new Error('View receipt mismatch.');
    },
    async clear(profileId: string): Promise<void> {
      const response = await request<{profile_id: string; cleared: number}>(
        `/profiles/${identifier(profileId)}/jobs/recently-viewed`, { method: 'DELETE' },
      );
      if (response.profile_id !== profileId || !Number.isInteger(response.cleared) ||
          response.cleared < 0 || response.cleared > 100) throw new Error('View history clear receipt mismatch.');
    },
  };
}

export const recentlyViewedClient = createRecentlyViewedClient();
