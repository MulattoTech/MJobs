import React, { useEffect, useRef, useState } from 'react';
import { api } from './api';
import {
  ApplicationFormReview,
} from './application-form-review';
import {
  inspectApplicationForm,
  parseApplicationFormReviewSnapshot,
  type ApplicationFormReviewSnapshot,
} from './application-form-inspection-client';

export { inspectApplicationForm, parseApplicationFormReviewSnapshot } from './application-form-inspection-client';
export type { ApplicationFormReviewSnapshot } from './application-form-inspection-client';

const emptySnapshot = (status: ApplicationFormReviewSnapshot['status']): ApplicationFormReviewSnapshot => ({
  status,
  fields: [],
});

type ReviewCapability = { status: 'supported' | 'unsupported' | 'error'; provider: string | null };
const capabilityError = (): ReviewCapability => ({ status: 'error', provider: null });

/** The server derives support from this application's current registry-normalized route. */
export function createApplicationFormReviewCapabilityClient(
  profileId: string,
  applicationId: string,
  fetcher: typeof fetch = globalThis.fetch,
  isScopeCurrent: () => boolean = () => true,
  signal?: AbortSignal,
) {
  const current = () => !signal?.aborted && isScopeCurrent();
  return {
    async load(): Promise<ReviewCapability> {
      if (!profileId.trim() || !applicationId.trim() || !current()) return capabilityError();
      try {
        const response = await fetcher(
          `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-review-capabilities`,
          { method: 'GET', cache: 'no-store', credentials: 'same-origin', signal },
        );
        if (!current() || !response.ok) return capabilityError();
        const body: unknown = await response.json();
        if (!current() || typeof body !== 'object' || body === null || Array.isArray(body)) return capabilityError();
        const value = body as Record<string, unknown>;
        if (value.schema_version !== 1 || value.profile_id !== profileId || value.application_id !== applicationId
            || value.read_only !== true || value.submission_supported !== false
            || typeof value.review_supported !== 'boolean') return capabilityError();
        if (!value.review_supported) {
          return value.provider === null ? { status: 'unsupported', provider: null } : capabilityError();
        }
        if (typeof value.provider !== 'string' || !/^[a-z][a-z0-9_]{0,63}$/.test(value.provider)) return capabilityError();
        return { status: 'supported', provider: value.provider };
      } catch {
        return capabilityError();
      }
    },
  };
}

/** Gate the existing read-only workspace without granting authentication or submission authority. */
export function ApplicationFormReviewCapabilityConnected({ profileId, applicationId, fetcher }: {
  profileId: string | null;
  applicationId: string | null;
  fetcher?: typeof fetch;
}) {
  const scopeKey = JSON.stringify([profileId, applicationId]);
  const selectedScope = useRef(scopeKey);
  selectedScope.current = scopeKey;
  const [capability, setCapability] = useState<(ReviewCapability & { scopeKey: string }) | null>(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setCapability(null);
    if (profileId && applicationId) {
      const client = createApplicationFormReviewCapabilityClient(profileId, applicationId, fetcher,
        () => active && selectedScope.current === scopeKey, controller.signal);
      void client.load().then(result => {
        if (active && selectedScope.current === scopeKey) setCapability({ ...result, scopeKey });
      });
    }
    return () => { active = false; controller.abort(); };
  }, [profileId, applicationId, scopeKey, fetcher]);

  if (!profileId || !applicationId) return <ApplicationFormReview status="unavailable" fields={[]} onInspect={() => {}} />;
  if (!capability || capability.scopeKey !== scopeKey) return <section className="panel" aria-label="Application form review">
    <h2>Application form review</h2>
    <p role="status">Checking read-only form review availability…</p>
  </section>;
  if (capability.status !== 'supported') return <ApplicationFormReview status="unavailable" fields={[]} onInspect={() => {}} />;
  return <ApplicationFormReviewConnected key={scopeKey} profileId={profileId} applicationId={applicationId} fetcher={fetcher} />;
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
      return parseApplicationFormReviewSnapshot(body, profileId, applicationId);
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
  const effectInstance = useRef<{ scopeKey: string; controller: AbortController } | null>(null);
  const [load, setLoad] = useState<ScopedLoad | null>(null);
  const [pendingScope, setPendingScope] = useState<string | null>(null);

  const inspect = () => {
    if (onInspect) {
      onInspect();
      return;
    }
    const instance = effectInstance.current;
    if (!profileId || !applicationId || !instance || instance.scopeKey !== scopeKey) return;
    setPendingScope(scopeKey);
    void inspectApplicationForm(api, profileId, applicationId, undefined, undefined, {
      signal: instance.controller.signal,
      isScopeCurrent: () => effectInstance.current === instance && selectedScope.current === scopeKey,
    }).then((snapshot) => {
      if (effectInstance.current === instance && selectedScope.current === scopeKey) setLoad({ ...snapshot, scopeKey });
    }).finally(() => {
      if (effectInstance.current === instance && selectedScope.current === scopeKey) setPendingScope(null);
    });
  };

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const instance = { scopeKey, controller };
    effectInstance.current = instance;
    setPendingScope(null);
    setLoad({ scopeKey, status: 'loading' });

    if (!profileId || !applicationId) {
      setLoad({ ...emptySnapshot('unavailable'), scopeKey });
      return () => { active = false; controller.abort(); };
    }

    const client = createApplicationFormReviewClient(
      profileId,
      applicationId,
      fetcher,
      () => active && effectInstance.current === instance && selectedScope.current === scopeKey,
      controller.signal,
    );
    void client.load().then((snapshot) => {
      if (active && selectedScope.current === scopeKey) setLoad({ ...snapshot, scopeKey });
    });

    return () => {
      active = false;
      controller.abort();
      if (effectInstance.current === instance) effectInstance.current = null;
    };
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
