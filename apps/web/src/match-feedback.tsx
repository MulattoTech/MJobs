import React, { useEffect, useRef, useState } from 'react';
import {
  MATCH_FEEDBACK_CATEGORIES,
  MatchFeedbackClientError,
  type DeterministicPreferenceProposal,
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
  savedFeedback: MatchFeedbackRecord | null;
  preferenceProposal: DeterministicPreferenceProposal | null;
  draftProposal: DeterministicPreferenceProposal | null;
  proposalEditing: boolean;
  onCategoryChange: (category: MatchFeedbackCategory | null) => void;
  onSave: () => void;
  onRetry: () => void;
  onReviewProposal: () => void;
  onProposalChange: (proposal: DeterministicPreferenceProposal) => void;
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
  savedFeedback,
  preferenceProposal,
  draftProposal,
  proposalEditing,
  onCategoryChange,
  onSave,
  onRetry,
  onReviewProposal,
  onProposalChange,
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
      <p><strong>Review required.</strong> This proposal is an editable draft only; it has not been applied. Reviewing or editing it
        does not change hard filters, match eligibility, or saved preferences.</p>
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
      {proposalEditing && draftProposal && <fieldset>
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
        <p className="muted">Edits stay in this draft. There is no apply or preference-save action here.</p>
      </fieldset>}
    </section>}
  </section>;
}

type Model = {
  scopeKey: string;
  category: MatchFeedbackCategory | null;
  phase: FeedbackPhase;
  error: string | null;
  retryable: boolean;
  history: MatchFeedbackRecord[];
  savedFeedback: MatchFeedbackRecord | null;
  preferenceProposal: DeterministicPreferenceProposal | null;
  draftProposal: DeterministicPreferenceProposal | null;
  proposalEditing: boolean;
};

function emptyModel(scopeKey: string): Model {
  return {
    scopeKey, category: null, phase: 'idle', error: null, retryable: false, history: [],
    savedFeedback: null, preferenceProposal: null, draftProposal: null,
    proposalEditing: false,
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
  const requestGeneration = useRef(0);

  useEffect(() => {
    requestGeneration.current += 1;
    requestContext.current = null;
    setModel(emptyModel(scopeKey));
    if (!profileId || !jobId || !client.load) return;
    const generation = requestGeneration.current;
    void client.load(profileId, jobId).then(history => {
      if (generation !== requestGeneration.current) return;
      setModel({ ...emptyModel(scopeKey), history: history.items });
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
    setModel({ ...visibleModel, scopeKey, draftProposal });
  };

  return <MatchFeedbackView
    profileId={profileId}
    jobId={jobId}
    category={visibleModel.category}
    phase={visibleModel.phase}
    error={visibleModel.error}
    retryable={visibleModel.retryable}
    history={visibleModel.history}
    savedFeedback={visibleModel.savedFeedback}
    preferenceProposal={visibleModel.preferenceProposal}
    draftProposal={visibleModel.draftProposal}
    proposalEditing={visibleModel.proposalEditing}
    onCategoryChange={chooseCategory}
    onSave={() => { void save(); }}
    onRetry={() => { void save(); }}
    onReviewProposal={() => setModel({ ...visibleModel, scopeKey, proposalEditing: true })}
    onProposalChange={updateProposal}
  />;
}
