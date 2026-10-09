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

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const MAX_DRAFT_CHARS = 256_000;

export function dynamicApplicationFormDeviceDraftStorageKey(
  profileId: string,
  applicationId: string,
  fingerprint: string,
): string {
  return `mjobs.dynamic-form.device.v1:${encodeURIComponent(profileId)}:${encodeURIComponent(applicationId)}:${fingerprint}`;
}

export function parseDynamicApplicationFormDraft(
  value: unknown,
  profileId: string,
  applicationId: string,
  fingerprint: string,
): DynamicApplicationRestoredDraft | undefined {
  if (!isRecord(value) || value.profile_id !== profileId || value.application_id !== applicationId
      || value.fingerprint !== fingerprint || !isRecord(value.answers)
      || Object.keys(value.answers).length > 200) return undefined;
  const answers: DynamicApplicationAnswers = {};
  let answerCharacters = 0;
  for (const [id, answer] of Object.entries(value.answers)) {
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(id) || ['__proto__', 'constructor', 'prototype'].includes(id)) return undefined;
    if (typeof answer === 'string' && answer.length <= 12_000) {
      answers[id] = answer;
      answerCharacters += answer.length;
    }
    else if (typeof answer === 'boolean') answers[id] = answer;
    else if (Array.isArray(answer) && answer.length <= 100
        && answer.every((item) => typeof item === 'string' && item.length <= 12_000)) {
      answers[id] = answer as string[];
      answerCharacters += answer.reduce((total, item) => total + (typeof item === 'string' ? item.length : 0), 0);
    } else return undefined;
    if (answerCharacters > MAX_DRAFT_CHARS) return undefined;
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

export function loadDynamicApplicationFormDeviceDraft(
  storage: DraftStorage,
  profileId: string,
  applicationId: string,
  fingerprint: string,
): DynamicApplicationRestoredDraft | undefined {
  try {
    const value = storage.getItem(dynamicApplicationFormDeviceDraftStorageKey(profileId, applicationId, fingerprint));
    if (!value || value.length > MAX_DRAFT_CHARS) return undefined;
    return parseDynamicApplicationFormDraft(JSON.parse(value), profileId, applicationId, fingerprint);
  } catch {
    return undefined;
  }
}

export function saveDynamicApplicationFormDeviceDraft(
  storage: DraftStorage,
  profileId: string,
  applicationId: string,
  fingerprint: string,
  draft: Pick<DynamicApplicationRestoredDraft, 'answers' | 'enabled_optional_sections'>,
): string {
  const value = JSON.stringify({ profile_id: profileId, application_id: applicationId, fingerprint, ...draft });
  if (value.length > MAX_DRAFT_CHARS
      || !parseDynamicApplicationFormDraft(JSON.parse(value), profileId, applicationId, fingerprint)) {
    throw new Error('Dynamic form draft is invalid or exceeds the local storage limit.');
  }
  const key = dynamicApplicationFormDeviceDraftStorageKey(profileId, applicationId, fingerprint);
  storage.setItem(key, value);
  return key;
}

export function clearDynamicApplicationFormDeviceDraft(
  storage: DraftStorage,
  profileId: string,
  applicationId: string,
  fingerprint: string,
): void {
  storage.removeItem(dynamicApplicationFormDeviceDraftStorageKey(profileId, applicationId, fingerprint));
}

export type DynamicApplicationFieldsClient = ReturnType<typeof createDynamicApplicationFieldsClient>;
type Loading = { scopeKey: string; status: 'loading' };
type Failed = { scopeKey: string; status: 'error'; message: string; stale?: boolean };
type Ready = { scopeKey: string; status: 'ready'; fields: DynamicApplicationFields;
  restoredDraft?: DynamicApplicationRestoredDraft; deviceDraftOnDevice?: boolean; draftGeneration?: number; message?: string };
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
  const [saveOnDevice, setSaveOnDevice] = useState(false);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoad({ scopeKey, status: 'loading' });
    setSaveOnDevice(false);
    if (!profileId.trim() || !applicationId.trim()) {
      setLoad({ scopeKey, status: 'error', message: 'Choose a profile and application to review dynamic fields.' });
      return () => { active = false; controller.abort(); };
    }
    void client.load(profileId, applicationId, controller.signal).then((fields) => {
      if (!active) return;
      let restoredDraft: DynamicApplicationRestoredDraft | undefined;
      let deviceDraftOnDevice = false;
      try {
        restoredDraft = loadDynamicApplicationFormDeviceDraft(window.localStorage,
          profileId, applicationId, fields.fingerprint);
        deviceDraftOnDevice = !!restoredDraft;
      } catch { /* Browser storage may be unavailable in restricted contexts. */ }
      try {
        const stored = restoredDraft ? null : window.sessionStorage.getItem(dynamicApplicationFormDraftStorageKey(
          profileId, applicationId, fields.fingerprint,
        ));
        if (stored && stored.length <= 256_000) {
          restoredDraft = parseDynamicApplicationFormDraft(
            JSON.parse(stored), profileId, applicationId, fields.fingerprint,
          );
        }
      } catch {
        // Leave the form empty when session recovery is unavailable.
      }
      setSaveOnDevice(deviceDraftOnDevice);
      setLoad({ scopeKey, status: 'ready', fields, restoredDraft, deviceDraftOnDevice, draftGeneration: 0 });
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
      const draft = { answers: payload.answers, enabled_optional_sections: payload.enabled_optional_sections };
      try {
        if (saveOnDevice) {
          saveDynamicApplicationFormDeviceDraft(window.localStorage, profileId, applicationId,
            current.fields.fingerprint, draft);
          window.sessionStorage.removeItem(key);
          setLoad({ ...current, deviceDraftOnDevice: true,
            message: 'Your answers are saved on this device for this application. They remain unreviewed if restored; nothing was sent to an employer.' });
        } else {
          clearDynamicApplicationFormDeviceDraft(window.localStorage, profileId, applicationId, current.fields.fingerprint);
          window.sessionStorage.setItem(key, JSON.stringify({
            profile_id: profileId,
            application_id: applicationId,
            fingerprint: current.fields.fingerprint,
            ...draft,
          }));
          setLoad({ ...current, deviceDraftOnDevice: false,
            message: 'Reviewed answers are saved in this browser tab for this application. Nothing was sent to an employer.' });
        }
      } catch {
        setLoad({ ...current,
          message: 'Answers could not be saved with the selected browser storage. Change the device-save choice and try Continue again; no employer request was made.' });
      }
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
    <p className="muted">Fields follow the saved form definition. You enter and review every answer. Answers stay on this device; MJobs does not fill, send, or submit the employer form. Local browser data may be readable by anyone with access to this browser profile.</p>
    <label className="consent"><input type="checkbox" aria-label="Save reviewed answers on this device" checked={saveOnDevice}
      onChange={event => setSaveOnDevice(event.target.checked)}/>
      Save reviewed answers on this device for recovery after closing this browser</label>
    <p className="muted">The local draft is written after you select Continue. Restored text must be reviewed again before continuing.</p>
    {current.deviceDraftOnDevice && !saveOnDevice &&
      <p className="muted">Continuing without device saving will remove the existing on-device copy.</p>}
    {current.deviceDraftOnDevice && <button type="button" className="quiet" aria-label="Clear saved application answers on this device"
      onClick={() => {
        try {
          clearDynamicApplicationFormDeviceDraft(window.localStorage, profileId, applicationId, current.fields.fingerprint);
          window.sessionStorage.removeItem(dynamicApplicationFormDraftStorageKey(profileId, applicationId, current.fields.fingerprint));
          setSaveOnDevice(false);
          setLoad({ ...current, restoredDraft: undefined, deviceDraftOnDevice: false,
            draftGeneration: (current.draftGeneration || 0) + 1, message: 'Saved local answers were cleared.' });
        } catch {
          setLoad({ ...current, message: 'Saved local answers could not be cleared from this browser.' });
        }
      }}>Clear saved local answers</button>}
    {current.fields.status === 'REVIEW_REQUIRED' &&
      <p role="status">Some fields or form rules need manual review before they can be used.</p>}
    {current.message && <p role="status">{current.message}</p>}
    <ApplicationDynamicFieldReview
      key={current.draftGeneration || 0}
      profileId={profileId}
      applicationId={applicationId}
      provider={provider}
      fields={current.fields.fields}
      restoredDraft={current.restoredDraft}
      onContinue={(payload) => { void onContinue(payload); }}
    />
  </section>;
}
