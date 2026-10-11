import React, { useEffect, useRef, useState } from 'react';
import { api } from './api';
import {
  ApplicationFormReview,
  type ApplicationFormReviewField,
} from './application-form-review';

export type ApplicationFormReviewSnapshot = {
  status: 'idle' | 'unavailable' | 'completed' | 'error';
  fields: ApplicationFormReviewField[];
};

const emptySnapshot = (status: ApplicationFormReviewSnapshot['status']): ApplicationFormReviewSnapshot => ({
  status,
  fields: [],
});

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const terminalStates = new Set(['COMPLETED', 'FAILED', 'DEAD_LETTERED', 'BLOCKED', 'CANCELLED', 'DISCARDED']);

type FormReviewApi = <T>(path: string, options?: RequestInit) => Promise<T>;

type FormReviewInspection = {
  status: 'completed' | 'unavailable' | 'error';
  fields: ApplicationFormReviewField[];
};

/** Queue an explicit read-only inspection and return only sanitized metadata. */
export async function inspectApplicationForm(
  request: FormReviewApi,
  profileId: string,
  applicationId: string,
  requestId: string = crypto.randomUUID(),
  delay: (milliseconds: number) => Promise<void> = milliseconds => new Promise(resolve => window.setTimeout(resolve, milliseconds)),
): Promise<FormReviewInspection> {
  try {
    await request<{ request_id: string; state: string; operation_id: string }>(
      `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews`,
      { method: 'POST', body: JSON.stringify({ request_id: requestId }) },
    );
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const result = await request<Record<string, unknown>>(
        `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews/${encodeURIComponent(requestId)}`,
      );
      if (typeof result.state === 'string' && terminalStates.has(result.state)) {
        const analysis = isRecord(result.analysis) ? result.analysis : null;
        if (result.state !== 'COMPLETED' || !analysis || !Array.isArray(analysis.fields)) {
          return { status: 'error', fields: [] };
        }
        if (analysis.status === 'UNKNOWN') return { status: 'unavailable', fields: [] };
        if (analysis.status !== 'ANALYZED' && analysis.status !== 'REVIEW_REQUIRED') {
          return { status: 'unavailable', fields: [] };
        }
        return {
          status: 'completed',
          fields: analysis.fields.filter(isRecord).map(field => ({
            name: typeof field.name === 'string' ? field.name : typeof field.id === 'string' ? field.id : 'Unnamed field',
            type: typeof field.type === 'string' ? field.type : 'other',
            required: field.required === true,
            reviewRequired: typeof field.review_reason === 'string' && field.review_reason.length > 0,
          })),
        };
      }
      if (attempt < 29) await delay(1000);
    }
    return { status: 'error', fields: [] };
  } catch {
    return { status: 'error', fields: [] };
  }
}

function parseLatestResponse(
  value: unknown,
  profileId: string,
  applicationId: string,
): ApplicationFormReviewSnapshot {
  if (!isRecord(value)) return emptySnapshot('error');
  if ((value.profile_id !== undefined && value.profile_id !== profileId)
      || (value.application_id !== undefined && value.application_id !== applicationId)) {
    return emptySnapshot('error');
  }
  if (typeof value.state !== 'string'
      || value.error_code !== null && typeof value.error_code !== 'string') {
    return emptySnapshot('error');
  }

  if (value.error_code !== null || value.state === 'ERROR' || value.state === 'FAILED') {
    return emptySnapshot('error');
  }
  if (value.state === 'IDLE') return emptySnapshot('idle');
  if (value.state === 'UNKNOWN') return emptySnapshot('unavailable');
  if (value.state !== 'COMPLETED') return emptySnapshot('unavailable');

  const analysis = value.analysis;
  if (!isRecord(analysis) || typeof analysis.status !== 'string') return emptySnapshot('error');
  if (analysis.status === 'UNKNOWN') return emptySnapshot('unavailable');
  if (analysis.status === 'ERROR' || analysis.status === 'FAILED') return emptySnapshot('error');
  if (analysis.status !== 'ANALYZED' && analysis.status !== 'REVIEW_REQUIRED') {
    return emptySnapshot('unavailable');
  }
  if (!Array.isArray(analysis.fields) || analysis.fields.length > 200) return emptySnapshot('error');

  const fields: ApplicationFormReviewField[] = [];
  for (const field of analysis.fields) {
    if (!isRecord(field) || typeof field.name !== 'string' || field.name.length > 500
        || typeof field.type !== 'string' || field.type.length > 80
        || typeof field.required !== 'boolean'
        || field.review_reason !== null && typeof field.review_reason !== 'string') {
      return emptySnapshot('error');
    }
    fields.push({
      name: field.name,
      type: field.type,
      required: field.required,
      reviewRequired: typeof field.review_reason === 'string' && field.review_reason.trim().length > 0,
    });
  }
  return { status: 'completed', fields };
}

/** Creates a read-only client for the latest durable inspection result in one selected scope. */
export function createApplicationFormReviewClient(
  profileId: string,
  applicationId: string,
  fetcher: typeof fetch = globalThis.fetch,
  isScopeCurrent: () => boolean = () => true,
  signal?: AbortSignal,
) {
  return {
    async load(): Promise<ApplicationFormReviewSnapshot> {
      if (!profileId.trim() || !applicationId.trim() || signal?.aborted || !isScopeCurrent()) {
        return emptySnapshot('error');
      }
      let response: Response;
      try {
        response = await fetcher(
          `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews/latest`,
          { method: 'GET', cache: 'no-store', credentials: 'same-origin', signal },
        );
      } catch {
        return emptySnapshot('error');
      }
      if (signal?.aborted || !isScopeCurrent() || !response.ok) return emptySnapshot('error');
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        return emptySnapshot('error');
      }
      if (signal?.aborted || !isScopeCurrent()) return emptySnapshot('error');
      return parseLatestResponse(body, profileId, applicationId);
    },
  };
}

type ScopedLoad =
  | { scopeKey: string; status: 'loading' }
  | ({ scopeKey: string } & ApplicationFormReviewSnapshot);

export function ApplicationFormReviewConnected({
  profileId,
  applicationId,
  onInspect,
  fetcher,
}: {
  profileId: string | null;
  applicationId: string | null;
  onInspect?: () => void;
  fetcher?: typeof fetch;
}) {
  const scopeKey = JSON.stringify([profileId, applicationId]);
  const selectedScope = useRef(scopeKey);
  selectedScope.current = scopeKey;
  const [load, setLoad] = useState<ScopedLoad | null>(null);
  const [pendingScope, setPendingScope] = useState<string | null>(null);

  const inspect = () => {
    if (onInspect) {
      onInspect();
      return;
    }
    if (!profileId || !applicationId) return;
    setPendingScope(scopeKey);
    void inspectApplicationForm(api, profileId, applicationId).then((snapshot) => {
      if (selectedScope.current === scopeKey) setLoad({ ...snapshot, scopeKey });
    }).finally(() => {
      if (selectedScope.current === scopeKey) setPendingScope(null);
    });
  };

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoad({ scopeKey, status: 'loading' });

    if (!profileId || !applicationId) {
      setLoad({ ...emptySnapshot('unavailable'), scopeKey });
      return () => { active = false; controller.abort(); };
    }

    const client = createApplicationFormReviewClient(
      profileId,
      applicationId,
      fetcher,
      () => active && selectedScope.current === scopeKey,
      controller.signal,
    );
    void client.load().then((snapshot) => {
      if (active && selectedScope.current === scopeKey) setLoad({ ...snapshot, scopeKey });
    });

    return () => { active = false; controller.abort(); };
  }, [scopeKey, profileId, applicationId, fetcher]);

  if (!profileId || !applicationId) {
    return <ApplicationFormReview status="unavailable" fields={[]} onInspect={inspect} />;
  }
  const current = load?.scopeKey === scopeKey ? load : null;
  if (!current || current.status === 'loading') {
    return <section className="panel" aria-label="Application form review">
      <h2>Application form review</h2>
      <p role="status" aria-live="polite">Loading saved form review…</p>
    </section>;
  }
  return <ApplicationFormReview status={pendingScope === scopeKey ? 'pending' : current.status} fields={current.fields} onInspect={inspect} />;
}
