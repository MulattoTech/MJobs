import React, { useEffect, useState } from 'react';
import { ApplicationDynamicFieldReview } from './application-dynamic-field-review';
import type { DynamicApplicationAnswers, DynamicApplicationContinuePayload, DynamicApplicationRestoredDraft } from './dynamic-application-form';
import {
  createDynamicApplicationFieldsClient,
  DynamicApplicationFieldsClientError,
  type DynamicApplicationFields,
} from './dynamic-application-form-client';

const defaultClient = createDynamicApplicationFieldsClient();
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export function dynamicApplicationFormDraftStorageKey(
  profileId: string,
  applicationId: string,
  fingerprint: string,
): string {
  return `mjobs.dynamic-form.v1:${encodeURIComponent(profileId)}:${encodeURIComponent(applicationId)}:${fingerprint}`;
}

export function parseDynamicApplicationFormDraft(
  value: unknown,
  profileId: string,
  applicationId: string,
  fingerprint: string,
): DynamicApplicationRestoredDraft | undefined {
  if (!isRecord(value) || value.profile_id !== profileId || value.application_id !== applicationId
      || value.fingerprint !== fingerprint || !isRecord(value.answers)) return undefined;
  const answers: DynamicApplicationAnswers = {};
  for (const [id, answer] of Object.entries(value.answers)) {
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(id)) return undefined;
    if (typeof answer === 'string' && answer.length <= 5000) answers[id] = answer;
    else if (typeof answer === 'boolean') answers[id] = answer;
    else if (Array.isArray(answer) && answer.length <= 100
        && answer.every((item) => typeof item === 'string' && item.length <= 500)) {
      answers[id] = answer as string[];
    } else return undefined;
  }
  const enabledSections = value.enabled_optional_sections;
  if (enabledSections !== undefined && (!Array.isArray(enabledSections) || enabledSections.length > 32
      || enabledSections.some((id) => typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(id)))) return undefined;
  return {
    profile_id: profileId,
    application_id: applicationId,
    answers,
    enabled_optional_sections: enabledSections as string[] | undefined,
  };
}

export type DynamicApplicationFieldsClient = ReturnType<typeof createDynamicApplicationFieldsClient>;
type Loading = { scopeKey: string; status: 'loading' };
type Failed = { scopeKey: string; status: 'error'; message: string; stale?: boolean };
type Ready = { scopeKey: string; status: 'ready'; fields: DynamicApplicationFields;
  restoredDraft?: DynamicApplicationRestoredDraft; message?: string };
type LoadState = Loading | Failed | Ready;

export function DynamicApplicationFormConnected({
  profileId,
  applicationId,
  provider,
  client = defaultClient,
}: {
  profileId: string;
  applicationId: string;
  provider: string;
  client?: DynamicApplicationFieldsClient;
}) {
  const scopeKey = JSON.stringify([profileId, applicationId]);
  const [load, setLoad] = useState<LoadState>({ scopeKey, status: 'loading' });
  const [reloadSequence, setReloadSequence] = useState(0);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoad({ scopeKey, status: 'loading' });
    if (!profileId.trim() || !applicationId.trim()) {
      setLoad({ scopeKey, status: 'error', message: 'Choose a profile and application to review dynamic fields.' });
      return () => { active = false; controller.abort(); };
    }
    void client.load(profileId, applicationId, controller.signal).then((fields) => {
      if (!active) return;
      let restoredDraft: DynamicApplicationRestoredDraft | undefined;
      try {
        const stored = window.sessionStorage.getItem(dynamicApplicationFormDraftStorageKey(
          profileId, applicationId, fields.fingerprint,
        ));
        if (stored && stored.length <= 256_000) {
          restoredDraft = parseDynamicApplicationFormDraft(
            JSON.parse(stored), profileId, applicationId, fields.fingerprint,
          );
        }
      } catch {
        restoredDraft = undefined;
      }
      setLoad({ scopeKey, status: 'ready', fields, restoredDraft });
    }).catch((error: unknown) => {
      if (!active || (error instanceof DOMException && error.name === 'AbortError')) return;
      const stale = error instanceof DynamicApplicationFieldsClientError && error.code === 'STALE';
      setLoad({ scopeKey, status: 'error', stale,
        message: error instanceof Error ? error.message
          : 'Dynamic application fields could not be loaded. Retry when the local service is available.' });
    });
    return () => { active = false; controller.abort(); };
  }, [scopeKey, profileId, applicationId, client, reloadSequence]);

  const current = load.scopeKey === scopeKey ? load : null;
  if (!current || current.status === 'loading') {
    return <section className="panel" aria-label="Dynamic application form review">
      <h2>Dynamic application form review</h2>
      <p role="status">Loading current profile-scoped form fields…</p>
    </section>;
  }
  if (current.status === 'error') {
    return <section className="panel" aria-label="Dynamic application form review">
      <h2>Dynamic application form review</h2>
      <p role="alert">{current.message}</p>
      <p className="muted">No form answer was sent or submitted.</p>
      <button type="button" onClick={() => setReloadSequence((value) => value + 1)}>
        {current.stale ? 'Reload changed form fields' : 'Retry loading form fields'}
      </button>
    </section>;
  }

  const onContinue = async (payload: DynamicApplicationContinuePayload) => {
    if (payload.profile_id !== profileId || payload.application_id !== applicationId) {
      setLoad({ scopeKey, status: 'error', message: 'The selected profile or application changed. Reload before continuing.' });
      return;
    }
    try {
      await client.load(profileId, applicationId, undefined, current.fields.fingerprint);
      const key = dynamicApplicationFormDraftStorageKey(profileId, applicationId, current.fields.fingerprint);
      window.sessionStorage.setItem(key, JSON.stringify({
        profile_id: profileId,
        application_id: applicationId,
        fingerprint: current.fields.fingerprint,
        answers: payload.answers,
        enabled_optional_sections: payload.enabled_optional_sections,
      }));
      setLoad({ ...current, message: 'Reviewed answers are saved in this browser tab for this application. Nothing was sent to an employer.' });
    } catch (error) {
      const stale = error instanceof DynamicApplicationFieldsClientError && error.code === 'STALE';
      setLoad({ scopeKey, status: 'error', stale,
        message: stale
          ? 'The application form changed after you reviewed it. Reload current fields and review the answers again.'
          : 'Your reviewed answers could not be preserved locally. Retry after checking this form.' });
    }
  };

  return <section className="panel" aria-label="Dynamic application form review">
    <h2>Dynamic application form review</h2>
    <p className="muted">Fields follow the saved form definition. You enter and review every answer. Answers stay in this browser tab; MJobs does not fill, send, or submit the employer form.</p>
    {current.fields.status === 'REVIEW_REQUIRED' &&
      <p role="status">Some fields or form rules need manual review before they can be used.</p>}
    {current.message && <p role="status">{current.message}</p>}
    <ApplicationDynamicFieldReview
      profileId={profileId}
      applicationId={applicationId}
      provider={provider}
      fields={current.fields.fields}
      restoredDraft={current.restoredDraft}
      onContinue={(payload) => { void onContinue(payload); }}
    />
  </section>;
}
