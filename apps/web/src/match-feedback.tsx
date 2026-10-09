import React, { useEffect, useRef, useState } from 'react';
import {
  MATCH_FEEDBACK_CATEGORIES,
  MatchFeedbackClientError,
  type DeterministicPreferenceProposal,
  type AppliedMatchPreference,
  type MatchFeedbackCategory,
  type MatchFeedbackRecord,
  type MatchFeedbackResult,
  type MatchFeedbackSubmitter,
  type PreferenceProposalDimension,
  type PreferenceProposalDirection,
} from './match-feedback-client';

export const FEEDBACK_CATEGORIES: ReadonlyArray<{ value: MatchFeedbackCategory; label: string }> = [
  { value: 'GOOD_MATCH', label: 'Good match' },
  { value: 'BAD_MATCH', label: 'Bad match' },
  { value: 'TOO_SENIOR', label: 'Too senior' },
  { value: 'WRONG_TECHNOLOGY', label: 'Wrong technology' },
  { value: 'WRONG_LOCATION', label: 'Wrong location' },
  { value: 'BAD_SALARY', label: 'Bad salary' },
  { value: 'BAD_COMPANY', label: 'Bad company' },
  { value: 'NOT_INTERESTED', label: 'Not interested' },
];

export type FeedbackPhase = 'idle' | 'loading' | 'error' | 'saved';

export type FeedbackRequestContext = {
  profileId: string;
  jobId: string;
  category: MatchFeedbackCategory;
  requestId: string;
};

export type PreferenceRequestContext = {
  profileId: string;
  feedbackId: string;
  dimension: PreferenceProposalDimension;
  direction: Exclude<PreferenceProposalDirection, 'review'>;
  requestId: string;
};

export function resolvePreferenceRequestId(
  current: PreferenceRequestContext | null,
  profileId: string,
  feedbackId: string,
  dimension: PreferenceProposalDimension,
  direction: Exclude<PreferenceProposalDirection, 'review'>,
  createRequestId: () => string,
): { context: PreferenceRequestContext; requestId: string } {
  if (current && current.profileId === profileId && current.feedbackId === feedbackId
    && current.dimension === dimension && current.direction === direction) {
    return { context: current, requestId: current.requestId };
  }
  const requestId = createRequestId();
  return {
    context: { profileId, feedbackId, dimension, direction, requestId },
    requestId,
  };
}

export function resolveFeedbackRequestId(
  current: FeedbackRequestContext | null,
  profileId: string,
  jobId: string,
  category: MatchFeedbackCategory,
  createRequestId: () => string,
): { context: FeedbackRequestContext; requestId: string } {
  if (current && current.profileId === profileId && current.jobId === jobId
    && current.category === category) {
    return { context: current, requestId: current.requestId };
  }
  const requestId = createRequestId();
  return {
    context: { profileId, jobId, category, requestId },
    requestId,
  };
}

export type MatchFeedbackViewProps = {
  profileId: string | null;
  jobId: string | null;
  category: MatchFeedbackCategory | null;
  phase: FeedbackPhase;
  error: string | null;
  retryable: boolean;
  history: MatchFeedbackRecord[];
  appliedPreferences: AppliedMatchPreference[];
  savedFeedback: MatchFeedbackRecord | null;
  preferenceProposal: DeterministicPreferenceProposal | null;
  draftProposal: DeterministicPreferenceProposal | null;
  proposalEditing: boolean;
  preferenceApplyPhase: 'idle' | 'saving' | 'saved' | 'error';
  preferenceApplyError: string | null;
  preferenceApplyRetryable: boolean;
  preferenceHistoryError: string | null;
  onCategoryChange: (category: MatchFeedbackCategory | null) => void;
  onSave: () => void;
  onRetry: () => void;
  onReviewProposal: () => void;
  onProposalChange: (proposal: DeterministicPreferenceProposal) => void;
  onApplyPreference: () => void;
  onRetryPreference: () => void;
};

function feedbackLabel(category: MatchFeedbackCategory): string {
  return FEEDBACK_CATEGORIES.find(option => option.value === category)?.label ?? 'Feedback';
}

const PROPOSAL_DIMENSIONS: Array<{ value: PreferenceProposalDimension; label: string }> = [
  { value: 'overall', label: 'Overall match' },
  { value: 'seniority', label: 'Seniority' },
  { value: 'technology', label: 'Technology' },
  { value: 'location', label: 'Location' },
  { value: 'salary', label: 'Salary' },
  { value: 'company', label: 'Company' },
];

export function MatchFeedbackView({
  profileId,
  jobId,
  category,
  phase,
  error,
  retryable,
  history,
  appliedPreferences,
  savedFeedback,
  preferenceProposal,
  draftProposal,
  proposalEditing,
  preferenceApplyPhase,
  preferenceApplyError,
  preferenceApplyRetryable,
  preferenceHistoryError,
  onCategoryChange,
  onSave,
  onRetry,
  onReviewProposal,
  onProposalChange,
  onApplyPreference,
  onRetryPreference,
}: MatchFeedbackViewProps) {
  const hasScope = !!profileId && !!jobId;
  const canSave = hasScope && category !== null && phase !== 'loading' && phase !== 'error' && phase !== 'saved';
  const updateProposal = (patch: Partial<DeterministicPreferenceProposal>) => {
    if (draftProposal) onProposalChange({ ...draftProposal, ...patch, review_required: true });
  };

  return <section className="panel" aria-label="Job match feedback">
    <div className="toolbar">
      <div><h2>Match feedback</h2><p>Share how useful this job match was.</p></div>
    </div>
    <p className="muted"><strong>This feedback does not change hard filters or job eligibility.</strong>
      It records your choice and may suggest a deterministic preference for review.</p>
    {!hasScope
      ? <p role="status">Select a profile and job to leave feedback.</p>
      : <>
        <label htmlFor="match-feedback-category">Feedback category</label>
        <select
          id="match-feedback-category"
          aria-label="Feedback category"
          value={category ?? ''}
          disabled={phase === 'loading'}
          onChange={event => onCategoryChange(
            (MATCH_FEEDBACK_CATEGORIES as readonly string[]).includes(event.currentTarget.value)
              ? event.currentTarget.value as MatchFeedbackCategory
              : null,
          )}
        >
          <option value="">Choose feedback</option>
          {FEEDBACK_CATEGORIES.map(option => <option key={option.value} value={option.value}>
            {option.label}
          </option>)}
        </select>
        <button type="button" aria-label="Save feedback" disabled={!canSave} onClick={onSave}>
          Save feedback
        </button>
      </>}

    {phase === 'loading' && <p role="status" aria-live="polite">Saving feedback and confirming it in saved history…</p>}
    {phase === 'error' && <div role="alert">
      <p>{error ?? 'Feedback could not be saved. Retry with the same request.'}</p>
      {retryable && <button type="button" aria-label="Retry feedback" onClick={onRetry}>
        Retry feedback
      </button>}
    </div>}
    {phase === 'saved' && savedFeedback && <p role="status" aria-live="polite">
      Feedback saved and confirmed by readback: <strong>{feedbackLabel(savedFeedback.category)}</strong>
      {' '}at <time dateTime={savedFeedback.created_at}>{savedFeedback.created_at}</time>.
    </p>}

    {history.length > 0 && <section aria-label="Saved feedback history">
      <h3>Saved feedback history</h3>
      <ol>{history.map(item => <li key={item.id}>
        <strong>{feedbackLabel(item.category)}</strong>{' '}
        <time dateTime={item.created_at}>{item.created_at}</time>
      </li>)}</ol>
      <p className="muted">Request identifiers are not included in feedback history.</p>
    </section>}

    {preferenceProposal && <section className="panel" aria-label="Preference proposal">
      <h3>Deterministic preference proposal</h3>
      <p><strong>Review required.</strong> This editable proposal only changes advisory priority after you explicitly apply it.
        It never changes hard filters, match eligibility, or application readiness.</p>
      <dl>
        <dt>Proposed dimension</dt>
        <dd>{draftProposal?.dimension ?? preferenceProposal.dimension}</dd>
        <dt>Proposed direction</dt>
        <dd>{draftProposal?.direction ?? preferenceProposal.direction}</dd>
        <dt>Reason</dt>
        <dd>{draftProposal?.explanation ?? preferenceProposal.explanation}</dd>
      </dl>
      <button type="button" aria-label="Review preference proposal" onClick={onReviewProposal}>
        Review preference proposal
      </button>
      {proposalEditing && draftProposal && <fieldset disabled={preferenceApplyPhase === 'saving'}>
        <legend>Edit proposal draft</legend>
        <label htmlFor="match-feedback-proposal-dimension">Proposed dimension</label>
        <select
          id="match-feedback-proposal-dimension"
          aria-label="Proposed dimension"
          value={draftProposal.dimension}
          onChange={event => {
            const value = event.currentTarget.value as PreferenceProposalDimension;
            if (PROPOSAL_DIMENSIONS.some(option => option.value === value)) updateProposal({ dimension: value });
          }}
        >
          {PROPOSAL_DIMENSIONS.map(option => <option key={option.value} value={option.value}>
            {option.label}
          </option>)}
        </select>
        <label htmlFor="match-feedback-proposal-direction">Proposed direction</label>
        <select
          id="match-feedback-proposal-direction"
          aria-label="Proposed direction"
          value={draftProposal.direction}
          onChange={event => {
            const value = event.currentTarget.value as PreferenceProposalDirection;
            if (['increase', 'decrease', 'review'].includes(value)) updateProposal({ direction: value });
          }}
        >
          <option value="increase">Increase preference</option>
          <option value="decrease">Decrease preference</option>
          <option value="review">Review evidence</option>
        </select>
        <label htmlFor="match-feedback-proposal-explanation">Proposal explanation</label>
        <textarea
          id="match-feedback-proposal-explanation"
          aria-label="Proposal explanation"
          maxLength={2000}
          value={draftProposal.explanation}
          onChange={event => updateProposal({ explanation: event.currentTarget.value })}
        />
        <p className="muted">Choose whether this factor should carry more or less weight in explainable application priority.</p>
      </fieldset>}
      {proposalEditing && savedFeedback && draftProposal
        && draftProposal.direction !== 'review' && <button type="button"
          aria-label="Apply reviewed preference"
          disabled={preferenceApplyPhase === 'saving'}
          onClick={onApplyPreference}>
          {preferenceApplyPhase === 'saving' ? 'Applying preference…' : 'Apply reviewed preference'}
        </button>}
      {preferenceApplyPhase === 'saving' && <p role="status">Saving and confirming this preference in profile history…</p>}
      {preferenceApplyPhase === 'saved' && <p role="status">Reviewed preference saved and confirmed. Application priority will explain its effect.</p>}
      {preferenceApplyPhase === 'error' && <div role="alert">
        <p>{preferenceApplyError ?? 'Preference could not be confirmed.'}</p>
        {preferenceApplyRetryable && <button type="button" aria-label="Retry preference apply" onClick={onRetryPreference}>
          Retry preference apply
        </button>}
      </div>}
    </section>}
    {appliedPreferences.length > 0 && <section aria-label="Applied profile preferences">
      <h4>Applied advisory preferences</h4>
      <ul>{appliedPreferences.map(item => <li key={item.id}>
        {item.dimension}: {item.direction.toLowerCase()} weight ({feedbackLabel(item.category)} feedback)
        {' '}<time dateTime={item.created_at}>{item.created_at}</time>
      </li>)}</ul>
    </section>}
    {preferenceHistoryError && <p role="alert">{preferenceHistoryError}</p>}
  </section>;
}

type Model = {
  scopeKey: string;
  category: MatchFeedbackCategory | null;
  phase: FeedbackPhase;
  error: string | null;
  retryable: boolean;
  history: MatchFeedbackRecord[];
  appliedPreferences: AppliedMatchPreference[];
  savedFeedback: MatchFeedbackRecord | null;
  preferenceProposal: DeterministicPreferenceProposal | null;
  draftProposal: DeterministicPreferenceProposal | null;
  proposalEditing: boolean;
  preferenceApplyPhase: 'idle' | 'saving' | 'saved' | 'error';
  preferenceApplyError: string | null;
  preferenceApplyRetryable: boolean;
  preferenceHistoryError: string | null;
};

function emptyModel(scopeKey: string): Model {
  return {
    scopeKey, category: null, phase: 'idle', error: null, retryable: false, history: [],
    appliedPreferences: [],
    savedFeedback: null, preferenceProposal: null, draftProposal: null,
    proposalEditing: false, preferenceApplyPhase: 'idle', preferenceApplyError: null,
    preferenceApplyRetryable: false, preferenceHistoryError: null,
  };
}

function newRequestId(): string {
  return globalThis.crypto.randomUUID();
}

export function MatchFeedback({
  profileId,
  jobId,
  client,
  createRequestId = newRequestId,
}: {
  profileId: string | null;
  jobId: string | null;
  client: MatchFeedbackSubmitter;
  createRequestId?: () => string;
}) {
  const scopeKey = JSON.stringify([profileId, jobId]);
  const [model, setModel] = useState<Model>(() => emptyModel(scopeKey));
  const visibleModel = model.scopeKey === scopeKey ? model : emptyModel(scopeKey);
  const requestContext = useRef<FeedbackRequestContext | null>(null);
  const preferenceRequestContext = useRef<PreferenceRequestContext | null>(null);
  const requestGeneration = useRef(0);

  useEffect(() => {
    requestGeneration.current += 1;
    requestContext.current = null;
    preferenceRequestContext.current = null;
    setModel(emptyModel(scopeKey));
    if (!profileId || !jobId || !client.load) return;
    const generation = requestGeneration.current;
    void Promise.all([
      client.load(profileId, jobId),
      client.loadPreferences
        ? client.loadPreferences(profileId).catch(() => null)
        : Promise.resolve<AppliedMatchPreference[] | null>([]),
    ]).then(([history, appliedPreferences]) => {
      if (generation !== requestGeneration.current) return;
      setModel({ ...emptyModel(scopeKey), history: history.items,
        appliedPreferences: appliedPreferences ?? [],
        preferenceHistoryError: appliedPreferences === null
          ? 'Applied preference history could not be loaded.' : null,
      });
    }).catch(error => {
      if (generation !== requestGeneration.current) return;
      setModel({ ...emptyModel(scopeKey), phase: 'error',
        error: error instanceof MatchFeedbackClientError ? error.message : 'Feedback history could not be loaded.',
        retryable: false });
    });
  }, [scopeKey, profileId, jobId, client]);

  const chooseCategory = (category: MatchFeedbackCategory | null) => {
    if (category === visibleModel.category) return;
    requestGeneration.current += 1;
    requestContext.current = null;
    setModel({
      ...visibleModel,
      scopeKey,
      category,
      phase: 'idle',
      error: null,
      retryable: false,
      savedFeedback: null,
      preferenceProposal: null,
      draftProposal: null,
      proposalEditing: false,
    });
  };

  const save = async () => {
    const selectedCategory = visibleModel.category;
    if (!profileId || !jobId || !selectedCategory || visibleModel.phase === 'loading'
      || visibleModel.phase === 'saved') return;
    const resolved = resolveFeedbackRequestId(
      requestContext.current, profileId, jobId, selectedCategory, createRequestId,
    );
    requestContext.current = resolved.context;
    const generation = ++requestGeneration.current;
    setModel({ ...visibleModel, scopeKey, phase: 'loading', error: null, retryable: false, savedFeedback: null });
    try {
      const result: MatchFeedbackResult = await client.submit(
        profileId, jobId, selectedCategory, resolved.requestId,
      );
      if (generation !== requestGeneration.current) return;
      if (result.profile_id !== profileId || result.job_id !== jobId
        || result.feedback.category !== selectedCategory
        || !result.items.some(item => item.id === result.feedback.id
          && item.category === selectedCategory)) {
        throw new MatchFeedbackClientError('SCOPE_MISMATCH', null, false);
      }
      setModel({
        ...visibleModel,
        scopeKey,
        phase: 'saved',
        error: null,
        retryable: false,
        history: result.items,
        savedFeedback: result.feedback,
        preferenceProposal: result.preference_proposal,
        draftProposal: result.preference_proposal,
        proposalEditing: false,
      });
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      setModel({
        ...visibleModel,
        scopeKey,
        phase: 'error',
        error: error instanceof MatchFeedbackClientError
          ? error.message
          : 'Feedback could not be saved. Retry with the same request.',
        retryable: error instanceof MatchFeedbackClientError ? error.retryable : true,
        savedFeedback: null,
      });
    }
  };

  const updateProposal = (draftProposal: DeterministicPreferenceProposal) => {
    preferenceRequestContext.current = null;
    setModel({ ...visibleModel, scopeKey, draftProposal, preferenceApplyPhase: 'idle',
      preferenceApplyError: null, preferenceApplyRetryable: false });
  };

  const applyPreference = async () => {
    const draft = visibleModel.draftProposal;
    const feedback = visibleModel.savedFeedback;
    if (!profileId || !feedback || !draft || !visibleModel.proposalEditing
      || draft.direction === 'review' || !client.applyPreference
      || visibleModel.preferenceApplyPhase === 'saving') return;
    const resolved = resolvePreferenceRequestId(
      preferenceRequestContext.current, profileId, feedback.id,
      draft.dimension, draft.direction, createRequestId,
    );
    preferenceRequestContext.current = resolved.context;
    const generation = ++requestGeneration.current;
    setModel({ ...visibleModel, scopeKey, preferenceApplyPhase: 'saving',
      preferenceApplyError: null, preferenceApplyRetryable: false });
    try {
      const appliedPreferences = await client.applyPreference(
        profileId, feedback.id, draft.dimension, draft.direction, resolved.requestId,
      );
      if (generation !== requestGeneration.current) return;
      setModel({ ...visibleModel, scopeKey, appliedPreferences, preferenceApplyPhase: 'saved',
        preferenceApplyError: null, preferenceApplyRetryable: false,
        preferenceHistoryError: null });
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      setModel({ ...visibleModel, scopeKey, preferenceApplyPhase: 'error',
        preferenceApplyError: error instanceof MatchFeedbackClientError
          ? error.message : 'Preference could not be saved. Retry with the same request.',
        preferenceApplyRetryable: error instanceof MatchFeedbackClientError ? error.retryable : true });
    }
  };

  return <MatchFeedbackView
    profileId={profileId}
    jobId={jobId}
    category={visibleModel.category}
    phase={visibleModel.phase}
    error={visibleModel.error}
    retryable={visibleModel.retryable}
    history={visibleModel.history}
    appliedPreferences={visibleModel.appliedPreferences}
    savedFeedback={visibleModel.savedFeedback}
    preferenceProposal={visibleModel.preferenceProposal}
    draftProposal={visibleModel.draftProposal}
    proposalEditing={visibleModel.proposalEditing}
    preferenceApplyPhase={visibleModel.preferenceApplyPhase}
    preferenceApplyError={visibleModel.preferenceApplyError}
    preferenceApplyRetryable={visibleModel.preferenceApplyRetryable}
    preferenceHistoryError={visibleModel.preferenceHistoryError}
    onCategoryChange={chooseCategory}
    onSave={() => { void save(); }}
    onRetry={() => { void save(); }}
    onReviewProposal={() => setModel({ ...visibleModel, scopeKey, proposalEditing: true })}
    onProposalChange={updateProposal}
    onApplyPreference={() => { void applyPreference(); }}
    onRetryPreference={() => { void applyPreference(); }}
  />;
}
