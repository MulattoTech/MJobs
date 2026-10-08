import React, { useEffect, useMemo, useRef, useState } from 'react';
import { RoleLensPicker, type RoleLensChoice, type RoleLensPickerProps, type RoleLensSelectionApiResponse } from './role-lens-picker';

export type { RoleLensSelectionApiResponse } from './role-lens-picker';

export type RoleLensApiCall = <T>(path: string, init?: RequestInit) => Promise<T>;

export type RoleLensSelectionSnapshot = Pick<RoleLensPickerProps,
  'roleLenses' | 'suggestedResponse' | 'selectedResponse' | 'requestedLens'
  | 'loading' | 'loadingError' | 'selecting' | 'selectionError'
>;

const emptySnapshot = (): RoleLensSelectionSnapshot => ({
  roleLenses: [],
  suggestedResponse: null,
  selectedResponse: null,
  requestedLens: null,
  loading: true,
  loadingError: null,
  selecting: false,
  selectionError: null,
});

export function roleLensSelectionPath(
  profileId: string,
  jobId: string,
  choice?: RoleLensChoice,
): string {
  const base = `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/role-lenses/selection`;
  if (!choice) return base;
  const query = new URLSearchParams({
    role_lens_id: choice.id,
    expected_role_lens_version: String(choice.version),
  });
  return `${base}?${query.toString()}`;
}

function responseMatchesContext(
  value: RoleLensSelectionApiResponse,
  profileId: string,
  jobId: string,
): boolean {
  return value.schema_version === 1
    && value.profile_id === profileId
    && value.job_id === jobId
    && typeof value.profile_version === 'string'
    && value.profile_version.length > 0
    && typeof value.job_fingerprint === 'string'
    && value.job_fingerprint.length > 0
    && Array.isArray(value.role_lenses)
    && !!value.selection
    && value.selection.profile_id === profileId
    && value.selection.profile_version === value.profile_version
    && Array.isArray(value.selection.source_refs?.facts)
    && Array.isArray(value.selection.source_refs?.stories);
}

function failureMessage(failure: unknown): string {
  return failure instanceof Error ? failure.message : String(failure);
}

/** Owns profile/job-scoped catalog loads and explicit, version-checked selection readbacks. */
export class RoleLensSelectionController {
  private state: RoleLensSelectionSnapshot = emptySnapshot();
  private profileId = '';
  private jobId = '';
  private sequence = 0;
  private listeners = new Set<(state: RoleLensSelectionSnapshot) => void>();
  private selectionChanged: (choice: RoleLensChoice | null) => void;

  constructor(private readonly api: RoleLensApiCall, onSelectionChange: (choice: RoleLensChoice | null) => void) {
    this.selectionChanged = onSelectionChange;
  }

  getSnapshot = (): RoleLensSelectionSnapshot => this.state;

  setSelectionCallback(callback: (choice: RoleLensChoice | null) => void): void {
    this.selectionChanged = callback;
  }

  subscribe(listener: (state: RoleLensSelectionSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private publish(): void {
    for (const listener of this.listeners) listener(this.state);
  }

  private clearSelection(): void {
    this.selectionChanged(null);
  }

  async setContext(profileId: string, jobId: string): Promise<void> {
    this.profileId = profileId;
    this.jobId = jobId;
    this.clearSelection();
    await this.load();
  }

  async retry(): Promise<void> {
    await this.load();
  }

  private async load(): Promise<void> {
    const request = ++this.sequence;
    const profileId = this.profileId;
    const jobId = this.jobId;
    this.state = emptySnapshot();
    this.publish();
    this.clearSelection();

    if (!profileId.trim() || !jobId.trim()) {
      this.state = {...this.state, loading: false, loadingError: 'Choose a profile and saved job before loading role lenses.'};
      this.publish();
      return;
    }

    try {
      const response = await this.api<RoleLensSelectionApiResponse>(roleLensSelectionPath(profileId, jobId));
      if (request !== this.sequence) return;
      if (!responseMatchesContext(response, profileId, jobId)) {
        throw new Error('The role-lens response did not match this profile and saved job. Reload the selection.');
      }
      this.state = {
        ...this.state,
        roleLenses: response.role_lenses,
        suggestedResponse: response,
      };
    } catch (failure) {
      if (request === this.sequence) {
        this.state = {...this.state, loadingError: failureMessage(failure)};
      }
    } finally {
      if (request === this.sequence) {
        this.state = {...this.state, loading: false};
        this.publish();
      }
    }
  }

  async select(choice: RoleLensChoice | null): Promise<void> {
    if (choice === null) {
      this.sequence += 1;
      this.state = {...this.state, requestedLens: null, selectedResponse: null, selecting: false, selectionError: null};
      this.publish();
      this.clearSelection();
      return;
    }

    const request = ++this.sequence;
    const profileId = this.profileId;
    const jobId = this.jobId;
    const baseline = this.state.suggestedResponse;
    this.state = {
      ...this.state,
      requestedLens: choice,
      selectedResponse: null,
      selecting: true,
      selectionError: null,
    };
    this.publish();
    this.clearSelection();

    const supported = this.state.roleLenses.some(lens => lens.id === choice.id && lens.version === choice.version);
    if (!supported || !baseline) {
      if (request === this.sequence) {
        this.state = {...this.state, selecting: false, selectionError: 'Reload the supported role-lens list before selecting this version.'};
        this.publish();
      }
      return;
    }

    try {
      const response = await this.api<RoleLensSelectionApiResponse>(roleLensSelectionPath(profileId, jobId, choice));
      if (request !== this.sequence) return;
      if (!responseMatchesContext(response, profileId, jobId)
          || response.profile_version !== baseline.profile_version
          || response.job_fingerprint !== baseline.job_fingerprint
          || response.selection.selection_reason !== 'EXPLICIT'
          || response.selection.lens.id !== choice.id
          || response.selection.lens.version !== choice.version
          || !response.role_lenses.some(lens => lens.id === choice.id && lens.version === choice.version)) {
        throw new Error('The selected lens readback no longer matches this profile, job, and version. Reload the role-lens list before continuing.');
      }
      this.state = {...this.state, selectedResponse: response};
      this.selectionChanged({id: response.selection.lens.id, version: response.selection.lens.version});
    } catch (failure) {
      if (request === this.sequence) {
        this.state = {...this.state, selectedResponse: null, selectionError: failureMessage(failure)};
        this.clearSelection();
      }
    } finally {
      if (request === this.sequence) {
        this.state = {...this.state, selecting: false};
        this.publish();
      }
    }
  }

  async retrySelection(): Promise<void> {
    if (this.state.requestedLens) await this.select(this.state.requestedLens);
  }

  dispose(): void {
    this.sequence += 1;
    this.clearSelection();
    this.listeners.clear();
  }
}

export type RoleLensSelectionLoaderProps = {
  api: RoleLensApiCall;
  profileId: string;
  jobId: string;
  onSelectionChange: (choice: RoleLensChoice | null) => void;
};

export function RoleLensSelectionLoader({ api, profileId, jobId, onSelectionChange }: RoleLensSelectionLoaderProps) {
  const contextKey = JSON.stringify([profileId, jobId]);
  const callback = useRef(onSelectionChange);
  callback.current = onSelectionChange;
  const controller = useMemo(
    () => new RoleLensSelectionController(api, choice => callback.current(choice)),
    [api],
  );
  const [observed, setObserved] = useState(() => ({contextKey, snapshot: controller.getSnapshot()}));
  const snapshot = observed.contextKey === contextKey ? observed.snapshot : emptySnapshot();

  useEffect(() => {
    const unsubscribe = controller.subscribe(next => setObserved({contextKey, snapshot: next}));
    void controller.setContext(profileId, jobId);
    return () => {
      unsubscribe();
      controller.dispose();
    };
  }, [controller, profileId, jobId]);

  return <RoleLensPicker
    {...snapshot}
    profileId={profileId}
    jobId={jobId}
    onRetry={() => { void controller.retry(); }}
    onRetrySelection={() => { void controller.retrySelection(); }}
    onSelectLens={choice => { void controller.select(choice); }}
  />;
}
