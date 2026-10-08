import React, { useEffect, useRef, useState } from 'react';

export type CareerStorySelectionAllowedUse =
  | 'RESUME'
  | 'COVER_LETTER'
  | 'APPLICATION_ANSWER'
  | 'INTERVIEW_PREPARATION';

export type CareerStorySelectionCandidate = {
  storyId: string;
  revisionId: string;
  revision: number;
  title: string;
  reviewStatus: string;
  reviewedAt: string | null;
  isLatest: boolean;
  reusable: boolean;
  allowedUses: readonly string[];
  factIds: readonly string[];
  factFingerprints: readonly {factId: string; fingerprint: string}[];
  staleFactIds: readonly string[];
};

export type CareerStoryRevisionChoice = {
  storyId: string;
  revisionId: string;
  revision: number;
};

export type CareerStorySelectionProps = {
  profileId: string;
  jobId: string;
  jobTitle?: string;
  allowedUse: CareerStorySelectionAllowedUse;
  candidates: readonly CareerStorySelectionCandidate[];
  approvedFactIds: readonly string[];
  onSelectionChange: (selection: readonly CareerStoryRevisionChoice[]) => void;
};

const useLabels: Record<CareerStorySelectionAllowedUse, string> = {
  RESUME: 'Resume',
  COVER_LETTER: 'Cover letter',
  APPLICATION_ANSWER: 'Application answer',
  INTERVIEW_PREPARATION: 'Interview preparation',
};

function uniqueCandidate(candidates: readonly CareerStorySelectionCandidate[], revisionId: string) {
  const matching = candidates.filter(candidate => candidate.revisionId === revisionId);
  return matching.length === 1 ? matching[0] : undefined;
}

export function careerStorySelectionUnavailableReason(
  candidate: CareerStorySelectionCandidate,
  allowedUse: CareerStorySelectionAllowedUse,
  approvedFactIds: readonly string[],
): string | null {
  if (!candidate.storyId.trim() || !candidate.revisionId.trim()
      || !Number.isSafeInteger(candidate.revision) || candidate.revision < 1) {
    return 'This revision does not have a valid exact identity.';
  }
  if (candidate.reviewStatus === 'REJECTED') return 'This revision was rejected.';
  if (candidate.reviewStatus !== 'APPROVED' || !candidate.reviewedAt) return 'This revision has not been approved.';
  if (!candidate.isLatest) return 'Only the current story revision can be reused.';
  if (candidate.staleFactIds.length > 0) return 'A linked fact is stale or unavailable.';
  if (!candidate.factIds.length) return 'This revision has no linked approved career facts.';
  const approved = new Set(approvedFactIds);
  if (candidate.factIds.some(factId => !approved.has(factId))) return 'A linked fact is not currently approved.';
  if (!candidate.allowedUses.includes(allowedUse)) {
    return `This story is not allowed for ${useLabels[allowedUse].toLowerCase()} use.`;
  }
  if (!candidate.reusable) return 'This revision is not currently reusable. Reload story and fact status.';
  return null;
}

export function isCareerStorySelectionCandidateEligible(
  candidate: CareerStorySelectionCandidate,
  allowedUse: CareerStorySelectionAllowedUse,
  approvedFactIds: readonly string[],
): boolean {
  return careerStorySelectionUnavailableReason(candidate, allowedUse, approvedFactIds) === null;
}

function selectedChoices(
  candidates: readonly CareerStorySelectionCandidate[],
  revisionIds: readonly string[],
  allowedUse: CareerStorySelectionAllowedUse,
  approvedFactIds: readonly string[],
): CareerStoryRevisionChoice[] {
  const uniqueIds = [...new Set(revisionIds)];
  return uniqueIds.flatMap(revisionId => {
    const candidate = uniqueCandidate(candidates, revisionId);
    return candidate && isCareerStorySelectionCandidateEligible(candidate, allowedUse, approvedFactIds)
      ? [{storyId: candidate.storyId, revisionId: candidate.revisionId, revision: candidate.revision}]
      : [];
  });
}

export function toggleCareerStorySelection(
  candidates: readonly CareerStorySelectionCandidate[],
  selectedRevisionIds: readonly string[],
  revisionId: string,
  checked: boolean,
  allowedUse: CareerStorySelectionAllowedUse,
  approvedFactIds: readonly string[],
): {selectedRevisionIds: string[]; selection: CareerStoryRevisionChoice[]} {
  const candidate = uniqueCandidate(candidates, revisionId);
  const eligible = !!candidate
    && isCareerStorySelectionCandidateEligible(candidate, allowedUse, approvedFactIds);
  const nextIds = checked
    ? eligible ? [...new Set([...selectedRevisionIds, revisionId])] : [...selectedRevisionIds]
    : selectedRevisionIds.filter(id => id !== revisionId);
  const selection = selectedChoices(candidates, nextIds, allowedUse, approvedFactIds);
  return {selectedRevisionIds: selection.map(choice => choice.revisionId), selection};
}

type SelectionState = {contextKey: string; revisionIds: string[]};

export function CareerStorySelection({
  profileId,
  jobId,
  jobTitle,
  allowedUse,
  candidates,
  approvedFactIds,
  onSelectionChange,
}: CareerStorySelectionProps) {
  const contextKey = JSON.stringify([profileId, jobId, allowedUse]);
  const callback = useRef(onSelectionChange);
  callback.current = onSelectionChange;
  const priorContext = useRef(contextKey);
  const [state, setState] = useState<SelectionState>(() => ({contextKey, revisionIds: []}));
  const revisionIds = state.contextKey === contextKey ? state.revisionIds : [];
  const validRevisionIds = selectedChoices(candidates, revisionIds, allowedUse, approvedFactIds)
    .map(choice => choice.revisionId);

  useEffect(() => {
    if (priorContext.current !== contextKey) {
      priorContext.current = contextKey;
      setState({contextKey, revisionIds: []});
      callback.current([]);
      return;
    }
    if (state.contextKey !== contextKey
        || JSON.stringify(state.revisionIds) === JSON.stringify(validRevisionIds)) return;
    const selection = selectedChoices(candidates, validRevisionIds, allowedUse, approvedFactIds);
    setState({contextKey, revisionIds: validRevisionIds});
    callback.current(selection);
  }, [contextKey, state, candidates, allowedUse, approvedFactIds, validRevisionIds]);

  function toggle(revisionId: string, checked: boolean) {
    const next = toggleCareerStorySelection(
      candidates, validRevisionIds, revisionId, checked, allowedUse, approvedFactIds,
    );
    setState({contextKey, revisionIds: next.selectedRevisionIds});
    callback.current(next.selection);
  }

  const title = jobTitle?.trim() || jobId;
  const revisionIdCounts = new Map<string, number>();
  for (const candidate of candidates) {
    revisionIdCounts.set(candidate.revisionId, (revisionIdCounts.get(candidate.revisionId) || 0) + 1);
  }
  const selectableCount = candidates.filter(candidate => (
    revisionIdCounts.get(candidate.revisionId) === 1
      && isCareerStorySelectionCandidateEligible(candidate, allowedUse, approvedFactIds)
  )).length;

  return <section className="panel" aria-label={`Career story selection for ${title}`} data-profile-id={profileId} data-job-id={jobId}>
    <h2>Story revisions for {title}</h2>
    <p>Choose exact, approved story revisions for this {useLabels[allowedUse].toLowerCase()}.</p>
    <p>{validRevisionIds.length} story revision{validRevisionIds.length === 1 ? '' : 's'} selected. Nothing is selected automatically.</p>
    {candidates.length
      ? <ul>{candidates.map((candidate, index) => {
        const reason = revisionIdCounts.get(candidate.revisionId) !== 1
          ? 'This revision identity is missing or ambiguous. Reload career stories.'
          : careerStorySelectionUnavailableReason(candidate, allowedUse, approvedFactIds);
        const disabled = reason !== null;
        const inputId = `career-story-selection-${index}`;
        const reasonId = `${inputId}-unavailable`;
        const selected = !disabled && validRevisionIds.includes(candidate.revisionId);
        const describedBy = disabled ? reasonId : undefined;
        return <li key={`${candidate.revisionId}-${index}`}>
          <label htmlFor={inputId}>
            <input
              id={inputId}
              type="checkbox"
              aria-label={`Use ${candidate.title} revision ${candidate.revision} for ${useLabels[allowedUse].toLowerCase()}`}
              aria-describedby={describedBy}
              checked={selected}
              disabled={disabled}
              onChange={event => toggle(candidate.revisionId, event.target.checked)}
            />
            <strong>{candidate.title}</strong> · Revision {candidate.revision}
          </label>
          <p>Review status: {candidate.reviewStatus}{candidate.reviewedAt ? ` · reviewed ${candidate.reviewedAt}` : ''}</p>
          {reason
            ? <p id={reasonId} role="status">Unavailable: {reason}</p>
            : <>
              <p>Allowed uses: {candidate.allowedUses.map(use => useLabels[use as CareerStorySelectionAllowedUse] || use).join(', ') || 'None'}</p>
              <p><strong>Approved fact provenance</strong></p>
              <ul>{candidate.factIds.map(factId => {
                const fingerprint = candidate.factFingerprints.find(item => item.factId === factId)?.fingerprint;
                return <li key={factId}>Approved fact reference: {factId}{fingerprint ? ` · fingerprint ${fingerprint}` : ''}</li>;
              })}</ul>
            </>}
        </li>;
      })}</ul>
      : <p>No career story revisions are available for this job.</p>}
    {candidates.length > 0 && selectableCount === 0 && <p role="status">No revisions are currently eligible for this document use.</p>}
  </section>;
}
