import type { ApplicationFormReviewField } from './application-form-review';

export type ApplicationFormReviewSnapshot = {
  status: 'idle' | 'unavailable' | 'completed' | 'error';
  fields: ApplicationFormReviewField[];
};

type ReviewIdentity = { requestId?: string; operationId?: string; routeId?: string };
type InspectionOptions = ReviewIdentity & { signal?: AbortSignal; isScopeCurrent?: () => boolean };
type ReviewRequest = <T>(path: string, options?: RequestInit) => Promise<T>;

const empty = (status: ApplicationFormReviewSnapshot['status']): ApplicationFormReviewSnapshot => ({ status, fields: [] });
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
// Match the durable worker's nullable identifier schema without accepting a trailing newline.
const safeFieldId = (value: unknown): value is string => typeof value === 'string'
  && value.match(/^[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]{0,99}$/)?.[0] === value;
const terminal = new Set(['COMPLETED', 'FAILED', 'DEAD_LETTERED', 'BLOCKED', 'CANCELLED', 'DISCARDED']);
const pending = new Set(['QUEUED', 'RUNNING', 'RETRY_PENDING', 'RETRYING', 'PENDING']);

function matchesScope(value: Record<string, unknown>, profileId: string, applicationId: string, expected: ReviewIdentity) {
  return (value.profile_id === undefined || value.profile_id === profileId)
    && (value.application_id === undefined || value.application_id === applicationId)
    && (expected.requestId === undefined || value.request_id === expected.requestId)
    && (value.operation_id === undefined || expected.operationId === undefined || value.operation_id === expected.operationId)
    && (value.route_id === undefined || expected.routeId === undefined || value.route_id === expected.routeId);
}

/** The wire format contains metadata only; never carry field values or raw errors into UI state. */
export function parseApplicationFormReviewSnapshot(
  value: unknown, profileId: string, applicationId: string, expected: ReviewIdentity = {},
): ApplicationFormReviewSnapshot {
  if (!record(value) || !matchesScope(value, profileId, applicationId, expected)
      || typeof value.state !== 'string'
      || value.error_code !== null && typeof value.error_code !== 'string') return empty('error');
  if (value.error_code !== null || terminal.has(value.state) && value.state !== 'COMPLETED'
      || value.state === 'ERROR') return empty('error');
  if (value.state === 'IDLE') return empty('idle');
  if (value.state !== 'COMPLETED') return empty('unavailable');
  if (!nonempty(value.request_id)) return empty('error');
  const analysis = value.analysis;
  if (!record(analysis) || typeof analysis.status !== 'string') return empty('error');
  if (analysis.status === 'UNKNOWN') return empty('unavailable');
  if (analysis.status === 'ERROR' || analysis.status === 'FAILED') return empty('error');
  if (analysis.status !== 'ANALYZED' && analysis.status !== 'REVIEW_REQUIRED') return empty('unavailable');
  if (!Array.isArray(analysis.fields) || analysis.fields.length > 200) return empty('error');
  const fields: ApplicationFormReviewField[] = [];
  for (const field of analysis.fields) {
    if (!record(field) || field.name !== null && (typeof field.name !== 'string' || field.name.length > 500)
        || field.name === null && field.id === undefined
        || field.id !== undefined && field.id !== null && !safeFieldId(field.id)
        || typeof field.type !== 'string' || field.type.length > 80
        || typeof field.required !== 'boolean'
        || field.review_reason !== null && typeof field.review_reason !== 'string') return empty('error');
    const name = nonempty(field.name) ? field.name : safeFieldId(field.id) ? field.id : 'Unnamed field';
    fields.push({ name, type: field.type, required: field.required,
      reviewRequired: typeof field.review_reason === 'string' && field.review_reason.trim().length > 0 });
  }
  return { status: 'completed', fields };
}

/** Start one explicit read-only review. A stale or cancelled scope cannot queue follow-up work. */
export async function inspectApplicationForm(
  request: ReviewRequest,
  profileId: string,
  applicationId: string,
  requestId: string = crypto.randomUUID(),
  delay: (milliseconds: number) => Promise<void> = milliseconds => new Promise(resolve => globalThis.setTimeout(resolve, milliseconds)),
  options: InspectionOptions = {},
): Promise<ApplicationFormReviewSnapshot> {
  const current = () => !options.signal?.aborted && (options.isScopeCurrent?.() ?? true);
  if (!profileId.trim() || !applicationId.trim() || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(requestId)
      || !current()) return empty('error');
  const path = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews`;
  try {
    const acknowledgement = await request<unknown>(path, {
      method: 'POST', body: JSON.stringify({ request_id: requestId }), signal: options.signal,
    });
    if (!current() || !record(acknowledgement)
        || !matchesScope(acknowledgement, profileId, applicationId, { ...options, requestId })
        || !nonempty(acknowledgement.operation_id) || !nonempty(acknowledgement.state)
        || acknowledgement.route_id !== undefined && !nonempty(acknowledgement.route_id)
        || !pending.has(acknowledgement.state) && !terminal.has(acknowledgement.state)) return empty('error');
    const expected = { ...options, requestId, operationId: acknowledgement.operation_id,
      routeId: options.routeId ?? (typeof acknowledgement.route_id === 'string' ? acknowledgement.route_id : undefined) };
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (!current()) return empty('error');
      const result = await request<unknown>(`${path}/${encodeURIComponent(requestId)}`, { signal: options.signal });
      if (!current() || !record(result) || !matchesScope(result, profileId, applicationId, expected)
          || typeof result.state !== 'string' || result.error_code !== null) return empty('error');
      if (terminal.has(result.state)) return parseApplicationFormReviewSnapshot(result, profileId, applicationId, expected);
      if (!pending.has(result.state)) return empty('error');
      if (attempt < 29) await delay(1000);
    }
  } catch {
    // Network/provider details can contain applicant data. Return only a fixed UI state.
  }
  return empty('error');
}
