import { useEffect, useReducer } from 'react';
import {
  type MatchingExplanation,
  type MatchingExplanationClient,
} from './matching-explanation-client';
import { MatchingExplanationPanel } from './matching-explanation';

export type MatchingExplanationWorkflowState = {
  scopeKey: string | null;
  state: 'loading' | 'error' | 'ready' | 'empty';
  explanation: MatchingExplanation | null;
  error: string | null;
  recovery: 'NONE' | 'REFRESH' | 'RETRY' | 'REFRESHING';
  attempt: number;
};

export type MatchingExplanationWorkflowEvent =
  | { type: 'SELECT'; scopeKey: string | null }
  | { type: 'LOADED'; scopeKey: string; explanation: MatchingExplanation }
  | {
      type: 'FAILED';
      scopeKey: string;
      error: string;
      recovery: 'NONE' | 'REFRESH' | 'RETRY';
    }
  | { type: 'RETRY' }
  | { type: 'REFRESH_STARTED'; scopeKey: string }
  | { type: 'REFRESH_FINISHED'; scopeKey: string }
  | { type: 'REFRESH_FAILED'; scopeKey: string };

export type MatchingExplanationWorkflowProps = {
  profileId: string | null;
  searchProfileId: string | null;
  jobId: string | null;
  client: MatchingExplanationClient;
  onRefresh: () => Promise<void>;
};

export function createMatchingExplanationScopeKey(
  profileId: string | null,
  searchProfileId: string | null,
  jobId: string | null,
): string | null {
  if (![profileId, searchProfileId, jobId].every(
    value => typeof value === 'string' && value.trim().length > 0,
  )) return null;
  return JSON.stringify([profileId, searchProfileId, jobId]);
}

function initialState(scopeKey: string | null): MatchingExplanationWorkflowState {
  return {
    scopeKey,
    state: scopeKey === null ? 'empty' : 'loading',
    explanation: null,
    error: null,
    recovery: 'NONE',
    attempt: 0,
  };
}

export function reduceMatchingExplanationWorkflow(
  state: MatchingExplanationWorkflowState,
  event: MatchingExplanationWorkflowEvent,
): MatchingExplanationWorkflowState {
  if (event.type === 'SELECT') {
    return {
      ...initialState(event.scopeKey),
      attempt: event.scopeKey === state.scopeKey ? state.attempt : 0,
    };
  }

  if (event.type === 'RETRY') {
    if (state.state !== 'error' || state.recovery !== 'RETRY') return state;
    return {
      ...state,
      state: 'loading',
      explanation: null,
      error: null,
      recovery: 'NONE',
      attempt: state.attempt + 1,
    };
  }

  if (event.type === 'REFRESH_STARTED') {
    if (event.scopeKey !== state.scopeKey || state.state !== 'error' || state.recovery !== 'REFRESH') {
      return state;
    }
    return { ...state, recovery: 'REFRESHING' };
  }

  if (event.type === 'REFRESH_FINISHED') {
    if (event.scopeKey !== state.scopeKey || state.recovery !== 'REFRESHING') return state;
    return {
      ...state,
      state: 'loading',
      explanation: null,
      error: null,
      recovery: 'NONE',
      attempt: state.attempt + 1,
    };
  }

  if (event.type === 'REFRESH_FAILED') {
    if (event.scopeKey !== state.scopeKey || state.recovery !== 'REFRESHING') return state;
    return {
      ...state,
      state: 'error',
      explanation: null,
      error: 'The match could not be refreshed. Check Activity and try again.',
      recovery: 'REFRESH',
    };
  }

  if (event.scopeKey !== state.scopeKey) return state;
  if (event.type === 'LOADED') {
    return {
      ...state,
      state: 'ready',
      explanation: event.explanation,
      error: null,
      recovery: 'NONE',
    };
  }
  return {
    ...state,
    state: 'error',
    explanation: null,
    error: event.error,
    recovery: event.recovery,
  };
}

type ClientFailure = {
  kind?: unknown;
  status?: unknown;
  retryable?: unknown;
};

function describeFailure(error: unknown): {
  message: string;
  recovery: 'NONE' | 'REFRESH' | 'RETRY';
} {
  const failure = error as ClientFailure | null;
  if (failure && (failure.kind === 'STALE_CONTEXT' || failure.status === 409)) {
    return {
      message: 'This saved explanation is out of date. Refresh profile matches to load current data for this job.',
      recovery: 'REFRESH',
    };
  }
  if (failure?.retryable === true) {
    return {
      message: 'The saved match explanation could not be loaded. You can retry this read.',
      recovery: 'RETRY',
    };
  }
  return {
    message: 'The saved match explanation is unavailable for this selection.',
    recovery: 'NONE',
  };
}

export function MatchingExplanationWorkflow({
  profileId,
  searchProfileId,
  jobId,
  client,
  onRefresh,
}: MatchingExplanationWorkflowProps) {
  const scopeKey = createMatchingExplanationScopeKey(profileId, searchProfileId, jobId);
  const [workflow, dispatch] = useReducer(
    reduceMatchingExplanationWorkflow,
    initialState(scopeKey),
  );

  // Mask the previous tuple during render, before the new effect starts.
  const visible = workflow.scopeKey === scopeKey ? workflow : initialState(scopeKey);

  useEffect(() => {
    if (scopeKey === null || profileId === null || searchProfileId === null || jobId === null) {
      dispatch({ type: 'SELECT', scopeKey: null });
      return;
    }

    let active = true;
    dispatch({ type: 'SELECT', scopeKey });
    void (async () => {
      try {
        const explanation = await client.load(profileId, searchProfileId, jobId);
        if (active) dispatch({ type: 'LOADED', scopeKey, explanation });
      } catch (error) {
        if (!active) return;
        const failure = describeFailure(error);
        dispatch({
          type: 'FAILED', scopeKey, error: failure.message, recovery: failure.recovery,
        });
      }
    })();

    return () => {
      active = false;
    };
  }, [client, profileId, searchProfileId, jobId, scopeKey, workflow.attempt]);

  const retry = () => dispatch({ type: 'RETRY' });
  const allowRetry = visible.state === 'error' && visible.recovery === 'RETRY';
  const allowRefresh = visible.state === 'error'
    && (visible.recovery === 'REFRESH' || visible.recovery === 'REFRESHING');

  const refreshMatches = async () => {
    if (!scopeKey || visible.recovery !== 'REFRESH') return;
    dispatch({ type: 'REFRESH_STARTED', scopeKey });
    try {
      await onRefresh();
      dispatch({ type: 'REFRESH_FINISHED', scopeKey });
    } catch {
      dispatch({ type: 'REFRESH_FAILED', scopeKey });
    }
  };

  return <div className="matching-explanation-workflow">
    <MatchingExplanationPanel
      profileId={profileId}
      searchProfileId={searchProfileId}
      jobId={jobId}
      state={visible.state}
      explanation={visible.explanation}
      error={visible.error}
      onRetry={allowRetry ? retry : undefined}
    />
    {allowRefresh && <button type="button" disabled={visible.recovery === 'REFRESHING'}
      onClick={() => void refreshMatches()}>
      {visible.recovery === 'REFRESHING' ? 'Refreshing profile matches…' : 'Refresh profile matches'}
    </button>}
  </div>;
}
