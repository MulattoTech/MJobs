import React, { useEffect, useMemo, useSyncExternalStore } from 'react';

export type RecruiterClassificationLabel =
  | 'DIRECT_EMPLOYER'
  | 'STAFFING_AGENCY'
  | 'RECRUITER'
  | 'UNKNOWN';

export type RecruiterClassificationEvidence = {
  signal: string;
  detail: string;
  source: string;
};

export type RecruiterClassification = {
  job_id: string;
  classification: RecruiterClassificationLabel;
  explanation: string;
  evidence: RecruiterClassificationEvidence[];
};

export type RecruiterClassificationClient = {
  classify: (jobId: string) => Promise<RecruiterClassification>;
};

export type RecruiterClassificationCardState = {
  jobId: string | null;
  result: RecruiterClassification | null;
  loading: boolean;
  error: string | null;
};

const initialState: RecruiterClassificationCardState = {
  jobId: null,
  result: null,
  loading: false,
  error: null,
};

const labels: Record<RecruiterClassificationLabel, string> = {
  DIRECT_EMPLOYER: 'DIRECT EMPLOYER',
  STAFFING_AGENCY: 'STAFFING AGENCY',
  RECRUITER: 'RECRUITER',
  UNKNOWN: 'UNKNOWN',
};

const validLabels = new Set<RecruiterClassificationLabel>([
  'DIRECT_EMPLOYER', 'STAFFING_AGENCY', 'RECRUITER', 'UNKNOWN',
]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const hasText = (value: unknown): value is string =>
  typeof value === 'string' && value.trim().length > 0;

function validateResult(value: unknown, jobId: string): RecruiterClassification {
  if (!isRecord(value) || value.job_id !== jobId || typeof value.classification !== 'string' ||
      !validLabels.has(value.classification as RecruiterClassificationLabel) ||
      !hasText(value.explanation) || !Array.isArray(value.evidence)) {
    throw new Error('Invalid recruiter classification response.');
  }
  const evidence = value.evidence.map((item): RecruiterClassificationEvidence => {
    if (!isRecord(item) || !hasText(item.signal) || !hasText(item.detail) || !hasText(item.source)) {
      throw new Error('Invalid recruiter classification evidence.');
    }
    return { signal: item.signal, detail: item.detail, source: item.source };
  });
  return {
    job_id: jobId,
    classification: value.classification as RecruiterClassificationLabel,
    explanation: value.explanation,
    evidence,
  };
}

/** Small request controller keeps job results scoped and exposes loading/retry states to the card. */
export class RecruiterClassificationController {
  private state: RecruiterClassificationCardState = initialState;
  private generation = 0;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly client: RecruiterClassificationClient) {}

  getSnapshot = (): RecruiterClassificationCardState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private publish(next: RecruiterClassificationCardState): void {
    this.state = next;
    this.listeners.forEach(listener => listener());
  }

  setJobId(jobId: string | null): void {
    const selected = jobId?.trim() || null;
    if (selected === this.state.jobId) return;
    this.generation += 1;
    this.publish({ ...initialState, jobId: selected });
  }

  async classify(jobId: string): Promise<void> {
    const selected = jobId.trim();
    if (!selected) return;
    const generation = ++this.generation;
    this.publish({ jobId: selected, result: null, loading: true, error: null });
    try {
      const result = validateResult(await this.client.classify(selected), selected);
      if (generation !== this.generation || this.state.jobId !== selected) return;
      this.publish({ jobId: selected, result, loading: false, error: null });
    } catch {
      if (generation !== this.generation || this.state.jobId !== selected) return;
      this.publish({
        jobId: selected,
        result: null,
        loading: false,
        error: 'Employer classification could not be loaded. Try again.',
      });
    }
  }

  async retry(): Promise<void> {
    if (this.state.jobId) await this.classify(this.state.jobId);
  }
}

type RecruiterClassificationCardViewProps = {
  state: RecruiterClassificationCardState;
  onClassify: () => void;
  onRetry: () => void;
};

export function RecruiterClassificationCardView({ state, onClassify, onRetry }: RecruiterClassificationCardViewProps) {
  const result = state.result && state.result.job_id === state.jobId ? state.result : null;
  return <section className="panel" aria-labelledby="recruiter-classification-heading" data-job-id={state.jobId ?? ''}>
    <h2 id="recruiter-classification-heading">Employer type classification</h2>
    <p className="muted">
      This classification is advisory and may be uncertain. It does not automatically reject staffing firms.
    </p>
    {state.loading
      ? <p role="status" aria-live="polite">Loading employer classification…</p>
      : state.error
        ? <div role="alert">
          <p>{state.error}</p>
          <button type="button" aria-label="Retry classification" disabled={!state.jobId} onClick={onRetry}>Retry classification</button>
        </div>
        : result
          ? <article aria-label="Recruiter classification result">
            <p><strong>Classification:</strong> {labels[result.classification]}</p>
            <h3>Why this classification</h3>
            <p>{result.explanation}</p>
            <h3>Evidence</h3>
            {result.evidence.length === 0
              ? <p className="muted">No classification evidence is available.</p>
              : <ul aria-label="Recruiter classification evidence">
                {result.evidence.map((item, index) => <li key={`${item.signal}-${index}`}>
                  <strong>{item.signal}</strong>: {item.detail} <span className="muted">Source: {item.source}</span>
                </li>)}
              </ul>}
          </article>
          : <p className="muted">No recruiter classification is available yet.</p>}
    {!state.error && <button type="button" disabled={state.loading || !state.jobId} onClick={onClassify}>
      {result ? 'Refresh classification' : 'Classify employer relationship'}
    </button>}
  </section>;
}

/** Explicitly requested, advisory-only classification card with an injected client. */
export function RecruiterClassificationCard({ jobId, client }: { jobId: string; client: RecruiterClassificationClient }) {
  const controller = useMemo(() => new RecruiterClassificationController(client), [client]);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  useEffect(() => controller.setJobId(jobId), [controller, jobId]);
  const viewState = state.jobId === jobId ? state : { ...initialState, jobId };

  return <RecruiterClassificationCardView
    state={viewState}
    onClassify={() => { void controller.classify(jobId); }}
    onRetry={() => { void controller.retry(); }}
  />;
}
