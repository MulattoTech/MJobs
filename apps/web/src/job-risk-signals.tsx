import React, { useEffect, useState } from 'react';

export type JobRiskSignal = {
  code: string;
  label: 'Potential Risk';
  severity: 'ADVISORY';
  reason: string;
  evidence_source: string;
  observed_value: string | null;
};

export type JobRiskUnknown = {
  code: string;
  reason: string;
};

export type JobRiskSignalsResponse = {
  profile_id: string;
  job_id: string;
  signals: JobRiskSignal[];
  unknowns: JobRiskUnknown[];
};

export type JobRiskSignalsClient = {
  get: (profileId: string, jobId: string) => Promise<JobRiskSignalsResponse>;
};

export type JobRiskSignalsState =
  | { status: 'loading' }
  | { status: 'error'; error: string }
  | { status: 'ready'; data: JobRiskSignalsResponse };

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function validateResponse(value: unknown, profileId: string, jobId: string): JobRiskSignalsResponse {
  if (!isRecord(value) || value.profile_id !== profileId || value.job_id !== jobId ||
      !Array.isArray(value.signals) || !Array.isArray(value.unknowns)) {
    throw new Error('Risk signals did not match the selected profile and job. Retry the risk check.');
  }
  const signals: JobRiskSignal[] = value.signals.map(signal => {
    if (!isRecord(signal) || typeof signal.code !== 'string' || !signal.code.trim() ||
        signal.label !== 'Potential Risk' || signal.severity !== 'ADVISORY' ||
        typeof signal.reason !== 'string' || !signal.reason.trim() ||
        typeof signal.evidence_source !== 'string' || !signal.evidence_source.trim() ||
        !(signal.observed_value === null || typeof signal.observed_value === 'string')) {
      throw new Error('Risk signals contained an invalid evidence record. Retry the risk check.');
    }
    return {
      code: signal.code,
      label: 'Potential Risk',
      severity: 'ADVISORY',
      reason: signal.reason,
      evidence_source: signal.evidence_source,
      observed_value: signal.observed_value,
    };
  });
  const unknowns: JobRiskUnknown[] = value.unknowns.map(unknown => {
    if (!isRecord(unknown) || typeof unknown.code !== 'string' || !unknown.code.trim() ||
        typeof unknown.reason !== 'string' || !unknown.reason.trim()) {
      throw new Error('Risk signals contained an invalid unknown status. Retry the risk check.');
    }
    return { code: unknown.code, reason: unknown.reason };
  });
  return { profile_id: profileId, job_id: jobId, signals, unknowns };
}

export async function requestJobRiskSignals(
  client: JobRiskSignalsClient,
  profileId: string,
  jobId: string,
): Promise<JobRiskSignalsResponse> {
  if (!profileId.trim() || !jobId.trim()) {
    throw new Error('Select a profile and job before checking risk signals.');
  }
  return validateResponse(await client.get(profileId, jobId), profileId, jobId);
}

type JobRiskSignalsPanelViewProps = {
  profileId: string;
  jobId: string;
  state: JobRiskSignalsState;
  onRetry: () => void;
};

export function JobRiskSignalsPanelView({
  profileId,
  jobId,
  state,
  onRetry,
}: JobRiskSignalsPanelViewProps) {
  return <section className="panel job-risk-signals" aria-labelledby="job-risk-signals-heading" data-profile-id={profileId} data-job-id={jobId}>
    <h2 id="job-risk-signals-heading">Potential Risk signals</h2>
    <p>These checks are heuristic indicators with limited evidence. They do not determine whether a posting is fraudulent and do not block or reject the posting.</p>
    {state.status === 'loading' && <p role="status" aria-busy="true">Loading risk signals…</p>}
    {state.status === 'error' && <>
      <p role="alert">{state.error}</p>
      <button type="button" onClick={onRetry}>Retry risk check</button>
    </>}
    {state.status === 'ready' && (state.data.profile_id !== profileId || state.data.job_id !== jobId)
      ? <p role="alert">Risk signals do not match this profile and job. Retry the risk check.</p>
      : null}
    {state.status === 'ready' && state.data.profile_id === profileId && state.data.job_id === jobId && <>
      {state.data.signals.length === 0
        ? <p>No potential risk signals were reported.</p>
        : <ul aria-label="Potential risk signals">
          {state.data.signals.map((signal, index) => <li key={`${signal.code}-${index}`}>
            <h3>{signal.label}</h3>
            <p>{signal.reason}</p>
            <p>Evidence source: {signal.evidence_source}</p>
            <p>Observed value: {signal.observed_value ?? 'Unknown'}</p>
            <p>Assessment: advisory heuristic only.</p>
          </li>)}
        </ul>}
      {state.data.unknowns.length > 0 && <>
        <h3>Unknown signals</h3>
        <ul aria-label="Unknown risk signals">
          {state.data.unknowns.map((unknown, index) => <li key={`${unknown.code}-${index}`}>
            <p>Unknown signal: {unknown.code}</p>
            <p>{unknown.reason}</p>
            <p>Unknown means the available evidence was insufficient; it is not a clear or unsafe result.</p>
          </li>)}
        </ul>
      </>}
      {state.data.signals.length === 0 && <p>This is not a guarantee that the posting is safe.</p>}
    </>}
  </section>;
}

type JobRiskSignalsPanelProps = {
  profileId: string;
  jobId: string;
  client: JobRiskSignalsClient;
};

export function JobRiskSignalsPanel({ profileId, jobId, client }: JobRiskSignalsPanelProps) {
  const [retryCount, setRetryCount] = useState(0);
  const scopeKey = `${profileId}\u0000${jobId}`;
  const [snapshot, setSnapshot] = useState<{ scopeKey: string; state: JobRiskSignalsState }>(() => ({
    scopeKey,
    state: { status: 'loading' },
  }));

  useEffect(() => {
    let active = true;
    setSnapshot({ scopeKey, state: { status: 'loading' } });
    void requestJobRiskSignals(client, profileId, jobId)
      .then(data => { if (active) setSnapshot({ scopeKey, state: { status: 'ready', data } }); })
      .catch(error => {
        if (!active) return;
        setSnapshot({
          scopeKey,
          state: {
            status: 'error',
            error: error instanceof Error ? error.message : 'Risk signals are unavailable. Retry the risk check.',
          },
        });
      });
    return () => { active = false; };
  }, [client, profileId, jobId, retryCount, scopeKey]);

  const currentState = snapshot.scopeKey === scopeKey ? snapshot.state : { status: 'loading' as const };
  return <JobRiskSignalsPanelView
    profileId={profileId}
    jobId={jobId}
    state={currentState}
    onRetry={() => setRetryCount(value => value + 1)}
  />;
}
