import React from 'react';
import {
  selectApplicationAnswerSuggestion,
  type ApplicationAnswerMatchCandidate,
  type ApplicationAnswerMatchResponse,
  type ApplicationAnswerMatchScope,
  type ApplicationAnswerSuggestionDraft,
} from './application-answer-matching-client';

export type ApplicationAnswerMatchingProps = {
  profileId: string;
  applicationId: string;
  question: string;
  response: ApplicationAnswerMatchResponse | null;
  onFindMatches: () => void;
  onSelectDraft: (draft: ApplicationAnswerSuggestionDraft) => void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
};

const methods: Record<ApplicationAnswerMatchCandidate['method'], string> = {
  EXACT_NORMALIZATION: 'Exact normalized match',
  FUZZY: 'Fuzzy match',
  CURATED_SEMANTIC: 'Curated semantic match',
};

const responseScopeMatches = (
  response: ApplicationAnswerMatchResponse,
  active: ApplicationAnswerMatchScope,
) => response.profile_id === active.profileId && response.application_id === active.applicationId &&
  response.question === active.question && response.scope.profileId === active.profileId &&
  response.scope.applicationId === active.applicationId && response.scope.question === active.question;

const isSelectableCandidate = (candidate: ApplicationAnswerMatchCandidate) =>
  candidate.verified && candidate.user_approved && candidate.confidence >= 80 && candidate.score >= 0.75;

export function ApplicationAnswerMatching({
  profileId,
  applicationId,
  question,
  response,
  onFindMatches,
  onSelectDraft,
  loading = false,
  error = null,
  onRetry,
}: ApplicationAnswerMatchingProps) {
  const activeScope = { profileId, applicationId, question };
  const scopeMatches = response !== null && responseScopeMatches(response, activeScope);
  const candidates = scopeMatches
    ? response.candidates.filter(isSelectableCandidate)
    : [];

  const selectCandidate = (candidate: ApplicationAnswerMatchCandidate) => {
    if (!response || !scopeMatches) return;
    const selected = selectApplicationAnswerSuggestion(candidate, response.scope, activeScope);
    if (selected.ok) onSelectDraft(selected.draft);
  };

  return <section aria-labelledby="application-answer-matching-heading">
    <h3 id="application-answer-matching-heading">Saved answer matches</h3>
    <p className="muted">
      Matches are suggestions based on saved questions. Review the source and answer for this application before using any text.
      Unreviewed draft: selecting one only adds draft text; it is not saved, approved, or sent.
    </p>
    <button type="button" aria-label="Find saved answer matches" disabled={loading || !profileId || !applicationId || !question.trim()} onClick={onFindMatches}>
      Find saved answer matches
    </button>

    {loading
      ? <p role="status" aria-live="polite">Loading saved answer matches…</p>
      : error
        ? <div role="alert">
          <p>{error}</p>
          {onRetry && <button type="button" aria-label="Retry saved answer matches" onClick={onRetry}>
            Retry saved answer matches
          </button>}
        </div>
        : response !== null && !scopeMatches
          ? <div role="alert"><p>These matches could not be safely matched to this profile, application, and question. Run the lookup again.</p></div>
          : response !== null && candidates.length === 0
            ? <p className="muted">No eligible saved answer matches were found.</p>
            : response !== null
              ? <ol aria-label="Saved answer match candidates">
                {candidates.map((candidate, index) => <li key={`${candidate.answer_id}-${index}`} aria-label={`Answer match ${index + 1}`}>
                  <h4>Rank {index + 1}</h4>
                  <p><strong>{methods[candidate.method]}</strong> · match score {Math.round(candidate.score * 100)}%</p>
                  <h5>Why it matched</h5>
                  <p>{candidate.explanation}</p>
                  <p>Matched against saved question: <q>{candidate.question}</q></p>
                  <p><strong>Saved answer:</strong> {candidate.text}</p>
                  <p><strong>Source:</strong> {candidate.source}</p>
                  <p><strong>Approval status:</strong> Verified and approved for reuse · {candidate.confidence}% confidence</p>
                  <button
                    type="button"
                    aria-label={`Select answer as draft: ${candidate.text}`}
                    onClick={() => selectCandidate(candidate)}
                  >
                    Select answer as draft
                  </button>
                </li>)}
              </ol>
              : <p className="muted">Run a lookup to review matching saved answers for this question.</p>}
  </section>;
}
