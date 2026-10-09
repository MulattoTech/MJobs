import React, { useEffect, useRef, useState } from 'react';
import {
  ApplicationMultipageReview,
  type ApplicationMultipageClient,
  type ApplicationMultipagePage,
  type ApplicationMultipageState,
} from './application-multipage-review';

export type { ApplicationMultipagePage, ApplicationMultipageState } from './application-multipage-review';

export type ApplicationMultipageWorkspaceErrorCode =
  | 'INVALID_REQUEST'
  | 'NETWORK_ERROR'
  | 'REQUEST_FAILED'
  | 'INVALID_RESPONSE'
  | 'SCOPE_MISMATCH'
  | 'STALE_SCOPE';

export class ApplicationMultipageWorkspaceError extends Error {
  constructor(readonly code: ApplicationMultipageWorkspaceErrorCode, message: string) {
    super(message);
    this.name = 'ApplicationMultipageWorkspaceError';
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function invalidResponse(): ApplicationMultipageWorkspaceError {
  return new ApplicationMultipageWorkspaceError(
    'INVALID_RESPONSE',
    'Saved application progress could not be validated. Reload before continuing.',
  );
}

function parseState(
  value: unknown,
  profileId: string,
  applicationId: string,
  pages: ApplicationMultipagePage[],
): ApplicationMultipageState {
  if (!isRecord(value)) throw invalidResponse();
  if (value.profile_id !== profileId || value.application_id !== applicationId) {
    throw new ApplicationMultipageWorkspaceError(
      'SCOPE_MISMATCH',
      'Saved progress belongs to a different profile or application. Reload the selected application.',
    );
  }

  const expectedOrder = pages.map((page) => page.id);
  const pageOrder = value.page_order;
  const version = value.version;
  const currentPageId = value.current_page_id;
  const status = value.status;
  const progress = value.progress;
  const uninitialized = version === 0 && currentPageId === null
    && Array.isArray(pageOrder) && pageOrder.length === 0;
  const validProgress = isRecord(progress) && Object.entries(progress).every(([pageId, answers]) =>
    expectedOrder.includes(pageId) && isRecord(answers)
      && Object.values(answers).every((answer) => typeof answer === 'string'));
  const validOrder = Array.isArray(pageOrder) && pageOrder.every((id) => typeof id === 'string')
    && (uninitialized || JSON.stringify(pageOrder) === JSON.stringify(expectedOrder));
  const validCurrentPage = uninitialized
    || (status === 'REVIEW' && currentPageId === 'review')
    || (status === 'IN_PROGRESS' && typeof currentPageId === 'string' && expectedOrder.includes(currentPageId));

  if (!Number.isInteger(version) || (version as number) < 0
      || (status !== 'IN_PROGRESS' && status !== 'REVIEW')
      || typeof value.updated_at !== 'string' && value.updated_at !== null
      || value.manual_intervention_required !== true
      || !validOrder || !validCurrentPage || !validProgress) {
    throw invalidResponse();
  }
  return value as unknown as ApplicationMultipageState;
}

/** Builds the profile/application-bound transport passed to the manual review UI. */
export function createApplicationMultipageClient(
  profileId: string,
  applicationId: string,
  pages: ApplicationMultipagePage[],
  fetcher: typeof fetch = globalThis.fetch,
  isScopeCurrent: () => boolean = () => true,
  signal?: AbortSignal,
): ApplicationMultipageClient {
  const assertCurrent = () => {
    if (signal?.aborted || !isScopeCurrent()) {
      throw new ApplicationMultipageWorkspaceError(
        'STALE_SCOPE',
        'The selected profile or application changed. Reload before continuing.',
      );
    }
  };

  const request = async (
    requestProfileId: string,
    requestApplicationId: string,
    init: RequestInit,
  ): Promise<ApplicationMultipageState> => {
    assertCurrent();
    if (requestProfileId !== profileId || requestApplicationId !== applicationId) {
      throw new ApplicationMultipageWorkspaceError(
        'STALE_SCOPE',
        'The selected profile or application changed. Reload before continuing.',
      );
    }
    if (!profileId.trim() || !applicationId.trim()) {
      throw new ApplicationMultipageWorkspaceError('INVALID_REQUEST', 'Choose a profile and application first.');
    }
    const path = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/multipage-state`;
    let response: Response;
    try {
      response = await fetcher(path, { ...init, signal });
    } catch {
      assertCurrent();
      throw new ApplicationMultipageWorkspaceError(
        'NETWORK_ERROR',
        'Could not load or save application progress. Check the local service and retry.',
      );
    }
    assertCurrent();
    if (!response.ok) {
      throw new ApplicationMultipageWorkspaceError(
        'REQUEST_FAILED',
        response.status === 404
          ? 'This application is unavailable for the selected profile.'
          : 'Application progress could not be saved. Reload and retry.',
      );
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      assertCurrent();
      throw invalidResponse();
    }
    assertCurrent();
    return parseState(body, requestProfileId, requestApplicationId, pages);
  };

  return {
    load: (profileId, applicationId) => request(profileId, applicationId, {
      method: 'GET',
      cache: 'no-store',
    }),
    transition: (profileId, applicationId, input) => request(profileId, applicationId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }),
  };
}

type WorkspaceSnapshot = {
  scopeKey: string;
  state: ApplicationMultipageState;
  client: ApplicationMultipageClient;
};

type WorkspaceLoad =
  | { scopeKey: string; status: 'loading' }
  | { scopeKey: string; status: 'error'; message: string }
  | ({ scopeKey: string; status: 'ready' } & WorkspaceSnapshot);

export function ApplicationMultipageWorkspace({
  profileId,
  applicationId,
  pages,
  fetcher,
}: {
  profileId: string | null;
  applicationId: string | null;
  pages: ApplicationMultipagePage[];
  fetcher?: typeof fetch;
}) {
  const pageKey = JSON.stringify(pages.map((page) => page.id));
  const scopeKey = JSON.stringify([profileId, applicationId, pageKey]);
  const selectedScope = useRef(scopeKey);
  selectedScope.current = scopeKey;
  const [load, setLoad] = useState<WorkspaceLoad | null>(null);
  const [reloadSequence, setReloadSequence] = useState(0);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoad({ scopeKey, status: 'loading' });

    if (!profileId || !applicationId) {
      setLoad({ scopeKey, status: 'error', message: 'Choose a profile and application to load saved progress.' });
      return () => {
        active = false;
        controller.abort();
      };
    }
    if (pages.length === 0 || new Set(pages.map((page) => page.id)).size !== pages.length) {
      setLoad({ scopeKey, status: 'error', message: 'Application pages are unavailable for this review.' });
      return () => {
        active = false;
        controller.abort();
      };
    }

    const client = createApplicationMultipageClient(
      profileId,
      applicationId,
      pages,
      fetcher,
      () => active && selectedScope.current === scopeKey,
      controller.signal,
    );
    void client.load(profileId, applicationId).then((state) => {
      if (active && selectedScope.current === scopeKey) {
        setLoad({ scopeKey, status: 'ready', state, client });
      }
    }).catch((reason: unknown) => {
      if (active && selectedScope.current === scopeKey) {
        const message = reason instanceof Error
          ? reason.message
          : 'Saved application progress could not be loaded. Reload to try again.';
        setLoad({ scopeKey, status: 'error', message });
      }
    });

    return () => {
      active = false;
      controller.abort();
    };
  }, [scopeKey, profileId, applicationId, pageKey, fetcher, reloadSequence]);

  if (!profileId || !applicationId) {
    return <section className="panel" aria-label="Multi-page application workspace">
      <p role="status">Choose a profile and application to load saved progress.</p>
    </section>;
  }
  const currentLoad = load?.scopeKey === scopeKey ? load : null;
  if (!currentLoad || currentLoad.status === 'loading') {
    return <section className="panel" aria-label="Multi-page application workspace">
      <p role="status">Loading saved application progress…</p>
    </section>;
  }
  if (currentLoad.status === 'error') {
    return <section className="panel" aria-label="Multi-page application workspace">
      <h2>Application review</h2>
      <p role="alert">{currentLoad.message}</p>
      <button type="button" onClick={() => setReloadSequence((value) => value + 1)}>Reload saved progress</button>
    </section>;
  }
  return <ApplicationMultipageReview
    profileId={profileId}
    applicationId={applicationId}
    pages={pages}
    initialState={currentLoad.state}
    client={currentLoad.client}
  />;
}
