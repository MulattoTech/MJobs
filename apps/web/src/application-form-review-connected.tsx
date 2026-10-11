import React, { useCallback, useState } from 'react';
import { api } from './api';
import { ApplicationFormReview, type ApplicationFormReviewField } from './application-form-review';

type ReviewResult = {
  request_id: string;
  state: string;
  error_code: string | null;
  analysis: null | {
    status: string;
    fields: Array<{ id?: string | null; name?: string | null; type: string; required: boolean; review_reason?: string | null }>;
  };
};

const terminalStates = new Set(['COMPLETED', 'FAILED', 'DEAD_LETTERED', 'BLOCKED', 'CANCELLED', 'DISCARDED']);

type FormReviewApi = <T>(path: string, options?: RequestInit) => Promise<T>;
type FormReviewInspection = { status: 'completed' | 'unavailable' | 'error'; fields: ApplicationFormReviewField[] };

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
      const result = await request<ReviewResult>(
        `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews/${encodeURIComponent(requestId)}`,
      );
      if (terminalStates.has(result.state)) {
        if (result.state !== 'COMPLETED' || !result.analysis) return { status: 'error', fields: [] };
        if (result.analysis.status === 'UNKNOWN') return { status: 'unavailable', fields: [] };
        return {
          status: 'completed',
          fields: result.analysis.fields.map(field => ({
            name: field.name || field.id || 'Unnamed field',
            type: field.type,
            required: field.required,
            reviewRequired: Boolean(field.review_reason),
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

export function ApplicationFormReviewConnected({ profileId, applicationId }: { profileId: string; applicationId: string }) {
  const [status, setStatus] = useState('idle');
  const [fields, setFields] = useState<ApplicationFormReviewField[]>([]);

  const inspect = useCallback(async () => {
    setStatus('pending');
    setFields([]);
    const result = await inspectApplicationForm(api, profileId, applicationId);
    setFields(result.fields);
    setStatus(result.status);
  }, [profileId, applicationId]);

  return <ApplicationFormReview status={status} fields={fields} onInspect={() => void inspect()}/>;
}
