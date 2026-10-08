import React, { useEffect } from 'react';

export type CareerStoryUse = 'RESUME' | 'COVER_LETTER' | 'APPLICATION_ANSWER' | 'INTERVIEW_PREPARATION';

export type CareerStoryReuseFact = {
  factId: string;
  kind: string;
  text: string;
  source: string;
  verified: boolean;
  userApproved: boolean;
};

export type CareerStoryReuseCandidate = {
  storyId: string;
  revisionId: string;
  revision: number;
  title: string;
  reviewStatus: 'PENDING' | 'APPROVED' | 'REJECTED';
  reviewedAt: string | null;
  isLatest: boolean;
  reusable: boolean;
  allowedUses: readonly string[];
  factIds: readonly string[];
  factFingerprints: readonly { factId: string; fingerprint: string }[];
  staleFactIds: readonly string[];
  provenance: readonly string[];
};

export type CareerStoryRevisionSelection = {
  storyId: string;
  revisionId: string;
  revision: number;
};

export type CareerStoryReusePickerProps = {
  use: CareerStoryUse;
  candidates: readonly CareerStoryReuseCandidate[];
  approvedFacts: readonly CareerStoryReuseFact[];
  selectedRevisionIds: readonly string[];
  onSelectionChange: (selection: readonly CareerStoryRevisionSelection[]) => void;
};

const useLabels: Record<CareerStoryUse, string> = {
  RESUME: 'Resume',
  COVER_LETTER: 'Cover letter',
  APPLICATION_ANSWER: 'Application answer',
  INTERVIEW_PREPARATION: 'Interview preparation',
};

export function careerStoryRevisionUnavailableReason(
  candidate: CareerStoryReuseCandidate,
  use: CareerStoryUse,
  approvedFacts: readonly CareerStoryReuseFact[],
): string | null {
  if (candidate.reviewStatus === 'REJECTED') return 'This revision was rejected.';
  if (candidate.reviewStatus !== 'APPROVED' || !candidate.reviewedAt) return 'This revision has not been approved.';
  if (!candidate.isLatest) return 'Only the current story revision can be reused.';
  if (candidate.staleFactIds.length) return 'A linked fact is stale or unavailable.';
  if (!candidate.allowedUses.includes(use)) return `This story is not allowed for ${useLabels[use].toLowerCase()} use.`;
  if (!candidate.factIds.length) return 'This revision has no linked approved career facts.';
  const linkedFacts = candidate.factIds.map(id => approvedFacts.find(fact => fact.factId === id));
  if (linkedFacts.some(fact => !fact || !fact.verified || !fact.userApproved)) {
    return 'A linked fact is not currently verified and user-approved.';
  }
  if (!candidate.reusable) return 'The saved revision is not currently reusable. Reload story and fact status.';
  return null;
}

export function isCareerStoryRevisionSelectable(
  candidate: CareerStoryReuseCandidate,
  use: CareerStoryUse,
  approvedFacts: readonly CareerStoryReuseFact[],
): boolean {
  return careerStoryRevisionUnavailableReason(candidate, use, approvedFacts) === null;
}

function selectionFor(candidate: CareerStoryReuseCandidate): CareerStoryRevisionSelection {
  return { storyId: candidate.storyId, revisionId: candidate.revisionId, revision: candidate.revision };
}

export function resolveCareerStoryRevisionSelection(
  candidates: readonly CareerStoryReuseCandidate[],
  selectedRevisionIds: readonly string[],
  use: CareerStoryUse,
  approvedFacts: readonly CareerStoryReuseFact[],
): CareerStoryRevisionSelection[] {
  const uniqueIds = [...new Set(selectedRevisionIds)];
  return uniqueIds.flatMap(id => {
    const candidate = candidates.find(item => item.revisionId === id);
    return candidate && isCareerStoryRevisionSelectable(candidate, use, approvedFacts) ? [selectionFor(candidate)] : [];
  });
}

export function CareerStoryReusePicker({ use, candidates, approvedFacts, selectedRevisionIds, onSelectionChange }: CareerStoryReusePickerProps) {
  const selectableCandidates = candidates.filter(candidate => isCareerStoryRevisionSelectable(candidate, use, approvedFacts));
  const validSelectedIds = [...new Set(selectedRevisionIds.filter(id => selectableCandidates.some(candidate => candidate.revisionId === id)))];
  const selectionChanged = JSON.stringify(validSelectedIds) !== JSON.stringify(selectedRevisionIds);

  useEffect(() => {
    if (selectionChanged) {
      onSelectionChange(resolveCareerStoryRevisionSelection(candidates, validSelectedIds, use, approvedFacts));
    }
  }, [selectionChanged, validSelectedIds, candidates, use, approvedFacts, onSelectionChange]);

  function toggle(candidate: CareerStoryReuseCandidate, checked: boolean) {
    if (!isCareerStoryRevisionSelectable(candidate, use, approvedFacts)) return;
    const nextIds = checked
      ? [...new Set([...validSelectedIds, candidate.revisionId])]
      : validSelectedIds.filter(id => id !== candidate.revisionId);
    onSelectionChange(resolveCareerStoryRevisionSelection(candidates, nextIds, use, approvedFacts));
  }

  return <section aria-label="Story reuse selection">
    <h3>Select story revisions for {useLabels[use].toLowerCase()}</h3>
    <p>Choose each exact revision you want to include. No stories are selected automatically.</p>
    <p>{validSelectedIds.length} story revision{validSelectedIds.length === 1 ? '' : 's'} selected.</p>
    {candidates.length
      ? <ul>{candidates.map(candidate => {
        const reason = careerStoryRevisionUnavailableReason(candidate, use, approvedFacts);
        const selected = reason === null && validSelectedIds.includes(candidate.revisionId);
        const describedBy = reason ? `story-revision-${candidate.revisionId}-unavailable` : undefined;
        return <li key={candidate.revisionId}>
          <label>
            <input
              type="checkbox"
              aria-label={`Use ${candidate.title} revision ${candidate.revision} for ${useLabels[use].toLowerCase()}`}
              aria-describedby={describedBy}
              checked={selected}
              disabled={reason !== null}
              onChange={event => toggle(candidate, event.target.checked)}
            />
            <span><strong>{candidate.title}</strong> · Revision {candidate.revision}</span>
          </label>
          <p>Review status: {candidate.reviewStatus}{candidate.reviewedAt ? ` · reviewed ${candidate.reviewedAt}` : ''}</p>
          <p>Allowed uses: {candidate.allowedUses.map(value => useLabels[value as CareerStoryUse] || value).join(', ') || 'None'}</p>
          <p><strong>Linked approved facts and provenance</strong></p>
          {candidate.factIds.length
            ? <ul>{candidate.factIds.map(factId => {
              const fact = approvedFacts.find(item => item.factId === factId);
              const fingerprint = candidate.factFingerprints.find(item => item.factId === factId)?.fingerprint;
              return <li key={factId}>
                {fact
                  ? <><strong>{fact.verified ? 'FACT VERIFIED' : 'FACT NOT VERIFIED'}</strong> · <strong>{fact.userApproved ? 'FACT USER-APPROVED' : 'FACT NOT USER-APPROVED'}</strong> · {fact.kind}: {fact.text}<br /><small>Provenance: {fact.source}</small></>
                  : <><strong>FACT NOT CURRENTLY AVAILABLE</strong> · {factId} is not in the current approved facts.</>}
                {fingerprint && <><br /><small>Linked fact fingerprint: {fingerprint}</small></>}
                {candidate.staleFactIds.includes(factId) && <><br /><strong>STALE FACT LINK</strong></>}
              </li>;
            })}</ul>
            : <p>No career facts are linked to this revision.</p>}
          {candidate.provenance.length > 0 && <p><strong>Story provenance references:</strong> {candidate.provenance.join('; ')}</p>}
          {reason && <p id={describedBy} role="status">Unavailable: {reason}</p>}
        </li>;
      })}</ul>
      : <p>No career story revisions are available for review.</p>}
  </section>;
}
