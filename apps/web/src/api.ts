export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request rejected (${response.status}). Check the fields and try again.`);
  return data as T;
}
export function eligible(fact: {verified: boolean; user_approved: boolean; confidence: number; source: string}) {
  return fact.verified && fact.user_approved && fact.confidence >= 80 && fact.source.trim().length > 0;
}
export const terminal = new Set(['COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED']);
