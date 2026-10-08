import React, { useEffect, useRef } from 'react';
import {
  careerStorySelectionUnavailableReason,
  type CareerStoryRevisionChoice,
  type CareerStorySelectionAllowedUse,
  type CareerStorySelectionCandidate,
} from './career-story-selection';

export type CareerStorySelectionCurrentness = 'CURRENT' | 'STALE' | 'REVOKED';

export type CareerStorySelectionReviewItem = {
  choice: CareerStoryRevisionChoice;
  candidate: CareerStorySelectionCandidate | null;
  currentness: CareerStorySelectionCurrentness;
  reason: string | null;
  linkedFactCount: number;
  verifiedFactCount: number;
};

export type CareerStorySelectionReview = {
  items: CareerStorySelectionReviewItem[];
  canPrepare: boolean;
};

export type CareerStorySelectionReviewProps = {
  selected: readonly CareerStoryRevisionChoice[];
  candidates: readonly CareerStorySelectionCandidate[];
  approvedFactIds: readonly string[];
  allowedUse: CareerStorySelectionAllowedUse;
  onRemove: (choice: CareerStoryRevisionChoice) => void;
  onClear: () => void;
  onPreparationReadinessChange: (canPrepare: boolean) => void;
};

const useLabels: Record<CareerStorySelectionAllowedUse, string> = {
  RESUME: 'Resume',
  COVER_LETTER: 'Cover letter',
  APPLICATION_ANSWER: 'Application answer',
  INTERVIEW_PREPARATION: 'Interview preparation',
};

function verifiedFactCount(candidate: CareerStorySelectionCandidate, approvedFactIds: readonly string[]): number {
  const approved = new Set(approvedFactIds);
  const uniqueIds = [...new Set(candidate.factIds)];
  return uniqueIds.filter(factId => {
    const matches = candidate.factFingerprints.filter(reference => reference.factId === factId);
    return approved.has(factId) && matches.length === 1 && matches[0]?.fingerprint.trim().length > 0;
  }).length;
}

function currentnessFor(
  choice: CareerStoryRevisionChoice,
  matches: readonly CareerStorySelectionCandidate[],
  candidate: CareerStorySelectionCandidate | undefined,
  allowedUse: CareerStorySelectionAllowedUse,
  approvedFactIds: readonly string[],
): {currentness: CareerStorySelectionCurrentness; reason: string | null} {
  if (matches.length !== 1 || !candidate
      || candidate.storyId !== choice.storyId
      || candidate.revisionId !== choice.revisionId
      || candidate.revision !== choice.revision) {
    return {currentness: 'STALE', reason: 'The selected revision no longer matches the current story record.'};
  }
  if (candidate.reviewStatus === 'REJECTED') {
    return {currentness: 'REVOKED', reason: 'This story revision was rejected.'};
  }
  if (candidate.reviewStatus !== 'APPROVED' || !candidate.reviewedAt) {
    return {currentness: 'REVOKED', reason: 'This story revision is not currently approved.'};
  }
  if (!candidate.allowedUses.includes(allowedUse)) {
    return {currentness: 'REVOKED', reason: `This story revision is not permitted for ${useLabels[allowedUse].toLowerCase()} use.`};
  }
  const unavailable = careerStorySelectionUnavailableReason(candidate, allowedUse, approvedFactIds);
  const linkedFactCount = new Set(candidate.factIds).size;
  if (unavailable || verifiedFactCount(candidate, approvedFactIds) !== linkedFactCount) {
    return {
      currentness: 'STALE',
      reason: unavailable || 'One or more linked facts no longer have current approved provenance.',
    };
  }
  return {currentness: 'CURRENT', reason: null};
}

export function assessCareerStorySelectionReview(
  selected: readonly CareerStoryRevisionChoice[],
  candidates: readonly CareerStorySelectionCandidate[],
  approvedFactIds: readonly string[],
  allowedUse: CareerStorySelectionAllowedUse,
): CareerStorySelectionReview {
  const items = selected.map(choice => {
    const matches = candidates.filter(candidate => candidate.revisionId === choice.revisionId);
    const candidate = matches.length === 1 ? matches[0] : undefined;
    const result = currentnessFor(choice, matches, candidate, allowedUse, approvedFactIds);
    return {
      choice: {...choice},
      candidate: candidate || null,
      currentness: result.currentness,
      reason: result.reason,
      linkedFactCount: candidate ? new Set(candidate.factIds).size : 0,
      verifiedFactCount: candidate ? verifiedFactCount(candidate, approvedFactIds) : 0,
    };
  });
  return {items, canPrepare: items.every(item => item.currentness === 'CURRENT')};
}

export function CareerStorySelectionReview({
  selected,
  candidates,
  approvedFactIds,
  allowedUse,
  onRemove,
  onClear,
  onPreparationReadinessChange,
}: CareerStorySelectionReviewProps) {
  const callback = useRef(onPreparationReadinessChange);
  callback.current = onPreparationReadinessChange;
  const review = assessCareerStorySelectionReview(selected, candidates, approvedFactIds, allowedUse);

  useEffect(() => {
    callback.current(review.canPrepare);
  }, [review.canPrepare]);

  return <section className="panel" aria-label="Selected career story review" data-preparation-ready={review.canPrepare}>
    <h2>Review selected story revisions</h2>
    <p>Permitted use: {useLabels[allowedUse]}. Story selection does not change the saved story or document.</p>
    {review.items.length === 0
      ? <p>No story revisions selected. You can prepare the cover letter without story reuse.</p>
      : <>
        <p>{review.items.length} exact story revision{review.items.length === 1 ? '' : 's'} selected.</p>
        {!review.canPrepare && <p role="alert">Remove or clear stale or revoked story revisions before preparation.</p>}
        <ul>{review.items.map(item => {
          const title = item.candidate?.title || item.choice.storyId;
          const currentness = item.currentness === 'CURRENT'
            ? 'Current'
            : item.currentness === 'REVOKED' ? 'Revoked' : 'Stale';
          const warning = item.currentness === 'REVOKED'
            ? 'This selected story revision is revoked or no longer approved. Remove it before preparation.'
            : 'This selected story revision is stale. Remove it before preparation.';
          return <li key={`${item.choice.storyId}:${item.choice.revisionId}`}>
            <strong>{title}</strong>
            <p>Story ID: {item.choice.storyId} · Revision ID: {item.choice.revisionId} · Revision {item.choice.revision}</p>
            <p>Permitted use: {useLabels[allowedUse]}</p>
            <p>Approval: {item.candidate?.reviewStatus || 'Unavailable'}{item.candidate?.reviewedAt ? ` · reviewed ${item.candidate.reviewedAt}` : ''}</p>
            <p>Currentness: {currentness}</p>
            <p>Verified fact provenance: {item.verifiedFactCount} of {item.linkedFactCount} linked facts</p>
            {item.currentness !== 'CURRENT' && <p role="alert">{warning} {item.reason}</p>}
            <button
              type="button"
              aria-label={`Remove ${title} revision ${item.choice.revision}`}
              onClick={() => onRemove(item.choice)}
            >Remove revision</button>
          </li>;
        })}</ul>
        <button type="button" aria-label="Clear selected career stories" onClick={onClear}>
          Clear selected stories
        </button>
      </>}
  </section>;
}
