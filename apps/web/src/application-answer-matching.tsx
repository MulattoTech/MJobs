import React from 'react';

export type ApplicationAnswerMatchMethod = 'EXACT_NORMALIZATION' | 'FUZZY' | 'CURATED_SEMANTIC';

export type ApplicationAnswerMatchCandidate = {
  answer_id: string;
  method: ApplicationAnswerMatchMethod;
  score: number;
  saved_question: string;
  answer: string;
  source: string;
  confidence: number;
  verified: boolean;
  user_approved: boolean;
};

export type ApplicationAnswerMatchResponse = {
  profile_id: string;
  application_id: string;
  question: string;
  candidates: ApplicationAnswerMatchCandidate[];
};

export type ApplicationAnswerMatchingProps = {
  profileId: string;
  applicationId: string;
  question: string;
  response: ApplicationAnswerMatchResponse | null;
  onFindMatches: () => void;
  onSelectDraft: (candidate: ApplicationAnswerMatchCandidate, question: string) => void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
};

const methods: Record<ApplicationAnswerMatchMethod, string> = {
  EXACT_NORMALIZATION: 'Exact normalized match',
  FUZZY: 'Fuzzy match',
  CURATED_SEMANTIC: 'Curated semantic match',
};

const isEligibleCandidate = (value: unknown): value is ApplicationAnswerMatchCandidate => {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ApplicationAnswerMatchCandidate>;
  return typeof candidate.answer_id === 'string' && candidate.answer_id.trim().length > 0 &&
    typeof candidate.method === 'string' && Object.hasOwn(methods, candidate.method) &&
    Number.isFinite(candidate.score) && candidate.score! >= 0 && candidate.score! <= 1 &&
    typeof candidate.saved_question === 'string' && candidate.saved_question.trim().length > 0 &&
    typeof candidate.answer === 'string' && candidate.answer.trim().length > 0 &&
    typeof candidate.source === 'string' && candidate.source.trim().length > 0 &&
    Number.isFinite(candidate.confidence) && candidate.confidence! >= 80 && candidate.confidence! <= 100 &&
    candidate.verified === true && candidate.user_approved === true;
};

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
  const scopeMatches = response !== null && response.profile_id === profileId &&
    response.application_id === applicationId && response.question === question;
  const candidates = scopeMatches && Array.isArray(response.candidates)
    ? response.candidates.filter(isEligibleCandidate)
    : [];

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
                  <p>Matched against saved question: <q>{candidate.saved_question}</q></p>
                  <p><strong>Saved answer:</strong> {candidate.answer}</p>
                  <p><strong>Source:</strong> {candidate.source}</p>
                  <p><strong>Approval status:</strong> Verified and approved for reuse · {candidate.confidence}% confidence</p>
                  <button
                    type="button"
                    aria-label={`Select answer as draft: ${candidate.answer}`}
                    onClick={() => onSelectDraft(candidate, question)}
                  >
                    Select answer as draft
                  </button>
                </li>)}
              </ol>
              : <p className="muted">Run a lookup to review matching saved answers for this question.</p>}
  </section>;
}
