import { api } from './api';
import type { CompanyPreferenceClient, CompanyPreferenceReadback, CompanyPreferenceStatus } from './company-preferences';

function route(profileId: string, companyId?: string): string {
  const base = `/profiles/${encodeURIComponent(profileId)}/company-preferences`;
  return companyId ? `${base}/${encodeURIComponent(companyId)}` : base;
}

export function createCompanyPreferenceClient(): CompanyPreferenceClient {
  const pendingRequestIds = new Map<string, string>();
  return {
    get: (profileId, companyId) => api<CompanyPreferenceReadback>(route(profileId, companyId)),
    set: async (profileId, companyId, status: CompanyPreferenceStatus) => {
      const key = `${profileId}\u0000${companyId}\u0000${status}`;
      const requestId = pendingRequestIds.get(key) || crypto.randomUUID();
      try {
        const result = await api<CompanyPreferenceReadback>(route(profileId, companyId), {
          method: 'PUT', body: JSON.stringify({ status, request_id: requestId }),
        });
        pendingRequestIds.delete(key);
        return result;
      } catch (error) {
        pendingRequestIds.set(key, requestId);
        throw error;
      }
    },
  };
}
