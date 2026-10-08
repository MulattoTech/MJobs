import React, { useState } from 'react';

export type ApprovedCareerFact = {
  factId: string;
  kind: string;
  text: string;
  source: string;
  verified: boolean;
  userApproved: boolean;
};

export type CareerStoryRevision = {
  revisionId: string;
  revision: number;
  createdAt: string;
  title: string;
  situation: string;
  task: string;
  action: string;
  result: string;
  factIds: readonly string[];
  allowedUses: readonly string[];
  factFingerprints?: readonly { factId: string; fingerprint: string }[];
  provenance?: readonly string[];
  reviewStatus: 'PENDING' | 'APPROVED' | 'REJECTED';
  reviewedAt: string | null;
  unstructuredText?: string | null;
  staleFactIds?: readonly string[];
  isLatest?: boolean;
};

export type CareerStory = {
  storyId: string;
  revisions: readonly CareerStoryRevision[];
};

export type CareerStoryRevisionInput = {
  storyId: string | null;
  title: string;
  situation: string;
  task: string;
  action: string;
  result: string;
  factIds: string[];
  allowedUses: string[];
};

export type CareerStoryRevisionResult = {
  storyId: string;
  revision: CareerStoryRevision;
};

export type CareerStoryBankProps = {
  stories: readonly CareerStory[];
  approvedFacts: readonly ApprovedCareerFact[];
  onCreateRevision: (input: CareerStoryRevisionInput) => Promise<CareerStoryRevisionResult>;
  onReviewRevision: (storyId: string, revisionId: string) => Promise<CareerStoryRevision>;
};

type StoryDraft = Omit<CareerStoryRevisionInput, 'storyId'>;

const emptyDraft: StoryDraft = {
  title: '', situation: '', task: '', action: '', result: '', factIds: [], allowedUses: [],
};

export const CAREER_STORY_ALLOWED_USES = [
  { value: 'RESUME', label: 'Resume' },
  { value: 'COVER_LETTER', label: 'Cover letter' },
  { value: 'APPLICATION_ANSWER', label: 'Application answer' },
  { value: 'INTERVIEW_PREPARATION', label: 'Interview preparation' },
] as const;

const allowedUseValues = new Set<string>(CAREER_STORY_ALLOWED_USES.map(use => use.value));

export function isStoryRevisionReusable(
  revision: Pick<CareerStoryRevision, 'reviewedAt' | 'factIds' | 'allowedUses' | 'staleFactIds' | 'isLatest' | 'reviewStatus'>,
  approvedFacts: readonly ApprovedCareerFact[],
): boolean {
  if (revision.reviewStatus !== 'APPROVED' || !revision.reviewedAt || revision.factIds.length === 0 || revision.allowedUses.length === 0
    || revision.allowedUses.some(use => !allowedUseValues.has(use))
    || revision.isLatest !== true || !revision.staleFactIds || revision.staleFactIds.length > 0) return false;
  return revision.factIds.every(id => approvedFacts.some(fact => fact.factId === id && fact.verified && fact.userApproved));
}

export function isStoryDraftSaveReady(
  draft: Pick<CareerStoryRevisionInput, 'factIds' | 'allowedUses'>,
  approvedFacts: readonly ApprovedCareerFact[],
): boolean {
  const selectedApprovedIds = draft.factIds.filter(id => approvedFacts.some(fact => fact.factId === id && fact.verified && fact.userApproved));
  return selectedApprovedIds.length > 0
    && draft.allowedUses.length > 0
    && draft.allowedUses.every(use => allowedUseValues.has(use));
}

function latestRevision(story: CareerStory): CareerStoryRevision | null {
  return [...story.revisions].sort((a, b) => b.revision - a.revision)[0] || null;
}

function toDraft(revision: CareerStoryRevision): StoryDraft {
  return {
    title: revision.title,
    situation: revision.situation,
    task: revision.task,
    action: revision.action,
    result: revision.result,
    factIds: [...revision.factIds],
    allowedUses: [...revision.allowedUses],
  };
}

function sameStoryContent(left: CareerStoryRevision, right: CareerStoryRevision): boolean {
  return left.revisionId === right.revisionId
    && left.revision === right.revision
    && left.createdAt === right.createdAt
    && left.title === right.title
    && left.situation === right.situation
    && left.task === right.task
    && left.action === right.action
    && left.result === right.result
    && JSON.stringify(left.factIds) === JSON.stringify(right.factIds)
    && JSON.stringify(left.allowedUses) === JSON.stringify(right.allowedUses)
    && JSON.stringify(left.factFingerprints || []) === JSON.stringify(right.factFingerprints || [])
    && JSON.stringify(left.staleFactIds || []) === JSON.stringify(right.staleFactIds || [])
    && (left.unstructuredText || '') === (right.unstructuredText || '')
    && JSON.stringify(left.provenance || []) === JSON.stringify(right.provenance || []);
}

export function CareerStoryBank({ stories: initialStories, approvedFacts, onCreateRevision, onReviewRevision }: CareerStoryBankProps) {
  const [stories, setStories] = useState<CareerStory[]>(() => initialStories.map(story => ({ ...story, revisions: [...story.revisions] })));
  const [selectedStoryId, setSelectedStoryId] = useState<string | null>(() => initialStories[0]?.storyId ?? null);
  const [draft, setDraft] = useState<StoryDraft>(() => {
    const revision = initialStories[0] ? latestRevision(initialStories[0]) : null;
    return revision ? toDraft(revision) : { ...emptyDraft, factIds: [] };
  });
  const [reviewConfirmations, setReviewConfirmations] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [reviewingRevisionId, setReviewingRevisionId] = useState<string | null>(null);
  const [error, setError] = useState('');

  const selectedStory = stories.find(story => story.storyId === selectedStoryId) || null;
  const reusableFacts = approvedFacts.filter(fact => fact.verified && fact.userApproved);
  const canSaveDraft = isStoryDraftSaveReady(draft, approvedFacts);
  const currentStoryId = selectedStory?.storyId ?? null;

  function startNewStory() {
    setSelectedStoryId(null);
    setDraft({ ...emptyDraft, factIds: [] });
    setError('');
  }

  function startRevision(story: CareerStory, revision: CareerStoryRevision) {
    setSelectedStoryId(story.storyId);
    setDraft(toDraft(revision));
    setError('');
  }

  async function saveRevision(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft.title.trim() || !draft.situation.trim() || !draft.task.trim() || !draft.action.trim() || !draft.result.trim()) {
      setError('Complete the title and all STAR fields before saving.');
      return;
    }
    if (!isStoryDraftSaveReady(draft, approvedFacts)) {
      setError('Select at least one allowed use and one currently verified, user-approved fact before saving.');
      return;
    }

    setSaving(true);
    setError('');
    try {
      const previousStory = selectedStory;
      const priorRevisions = previousStory?.revisions || [];
      const input: CareerStoryRevisionInput = {
        storyId: currentStoryId,
        title: draft.title.trim(),
        situation: draft.situation.trim(),
        task: draft.task.trim(),
        action: draft.action.trim(),
        result: draft.result.trim(),
        factIds: draft.factIds.filter(id => reusableFacts.some(fact => fact.factId === id)),
        allowedUses: [...draft.allowedUses],
      };
      const result = await onCreateRevision(input);
      const nextRevision = result.revision;
      const priorIds = new Set(priorRevisions.map(revision => revision.revisionId));
      const expectedNumber = Math.max(0, ...priorRevisions.map(revision => revision.revision)) + 1;
      const returnedFactIds = [...nextRevision.factIds].sort();
      const expectedFactIds = [...input.factIds].sort();
      if (!result.storyId || result.storyId !== (previousStory?.storyId || result.storyId)
        || !nextRevision.revisionId || priorIds.has(nextRevision.revisionId)
        || nextRevision.revision !== expectedNumber || nextRevision.reviewedAt !== null || nextRevision.reviewStatus !== 'PENDING'
        || nextRevision.title !== input.title || nextRevision.situation !== input.situation
        || nextRevision.task !== input.task || nextRevision.action !== input.action
        || nextRevision.result !== input.result || JSON.stringify(returnedFactIds) !== JSON.stringify(expectedFactIds)
        || JSON.stringify([...nextRevision.allowedUses].sort()) !== JSON.stringify([...input.allowedUses].sort())) {
        throw new Error('The save response did not confirm a new, unreviewed revision. Reload story history before trying again.');
      }

      const updatedStory: CareerStory = {
        storyId: result.storyId,
        revisions: [...priorRevisions, nextRevision],
      };
      setStories(previous => previousStory
        ? previous.map(story => story.storyId === previousStory.storyId ? updatedStory : story)
        : [...previous, updatedStory]);
      setSelectedStoryId(result.storyId);
      setReviewConfirmations(previous => ({ ...previous, [nextRevision.revisionId]: false }));
      setDraft(toDraft(nextRevision));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSaving(false);
    }
  }

  async function reviewRevision(story: CareerStory, revision: CareerStoryRevision) {
    setReviewingRevisionId(revision.revisionId);
    setError('');
    try {
      const reviewed = await onReviewRevision(story.storyId, revision.revisionId);
      if (!sameStoryContent(revision, reviewed) || !reviewed.reviewedAt || reviewed.reviewStatus !== 'APPROVED') {
        throw new Error('Review did not confirm this exact saved revision. It remains unavailable for reuse.');
      }
      setStories(previous => previous.map(item => item.storyId === story.storyId
        ? { ...item, revisions: item.revisions.map(itemRevision => itemRevision.revisionId === revision.revisionId ? reviewed : itemRevision) }
        : item));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setReviewingRevisionId(null);
    }
  }

  function updateDraft(field: Exclude<keyof StoryDraft, 'factIds'>, value: string) {
    setDraft(previous => ({ ...previous, [field]: value }));
  }

  function updateFact(factId: string, checked: boolean) {
    setDraft(previous => ({
      ...previous,
      factIds: checked
        ? [...new Set([...previous.factIds, factId])]
        : previous.factIds.filter(id => id !== factId),
    }));
  }

  function updateAllowedUse(use: string, checked: boolean) {
    setDraft(previous => ({
      ...previous,
      allowedUses: checked
        ? [...new Set([...previous.allowedUses, use])]
        : previous.allowedUses.filter(value => value !== use),
    }));
  }

  return <section aria-label="Career story bank">
    <header>
      <h2>Career story bank</h2>
      <p>Build reusable stories from your own approved career facts. Each saved edit creates a new revision.</p>
    </header>

    <div aria-label="Saved career stories">
      <h3>Saved stories</h3>
      {stories.length
        ? <ul>{stories.map(story => {
          const latest = latestRevision(story);
          if (!latest) return null;
          return <li key={story.storyId}>
            <button type="button" aria-pressed={selectedStoryId === story.storyId} onClick={() => startRevision(story, latest)}>{latest.title}</button>
            <span> · {story.revisions.length} revision{story.revisions.length === 1 ? '' : 's'}</span>
          </li>;
        })}</ul>
        : <p>No career stories yet.</p>}
      <button type="button" onClick={startNewStory}>New career story</button>
    </div>

    <form onSubmit={event => void saveRevision(event)}>
      <h3>{selectedStory ? `Edit ${draft.title || 'career story'} as a new revision` : 'New career story'}</h3>
      <p>A saved revision is unavailable for reuse until you review that exact revision.</p>
      <label>Story title<input aria-label="Story title" value={draft.title} maxLength={160} required onChange={event => updateDraft('title', event.target.value)} /></label>
      <label>Situation<textarea aria-label="Situation" value={draft.situation} maxLength={5000} required onChange={event => updateDraft('situation', event.target.value)} /></label>
      <label>Task<textarea aria-label="Task" value={draft.task} maxLength={5000} required onChange={event => updateDraft('task', event.target.value)} /></label>
      <label>Action<textarea aria-label="Action" value={draft.action} maxLength={10000} required onChange={event => updateDraft('action', event.target.value)} /></label>
      <label>Result<textarea aria-label="Result" value={draft.result} maxLength={5000} required onChange={event => updateDraft('result', event.target.value)} /></label>

      <fieldset>
        <legend>Link approved career facts</legend>
        {reusableFacts.length
          ? reusableFacts.map(fact => <label key={fact.factId}>
            <input
              type="checkbox"
              aria-label={`Link approved fact: ${fact.text}`}
              checked={draft.factIds.includes(fact.factId)}
              onChange={event => updateFact(fact.factId, event.target.checked)}
            />
            <span><strong>FACT VERIFIED</strong> · <strong>FACT USER-APPROVED</strong> · {fact.kind}: {fact.text}<br /><small>Provenance: {fact.source}</small></span>
          </label>)
          : <p>No verified, user-approved career facts are available to link. Select a current approved fact before saving a story revision.</p>}
      </fieldset>

      <fieldset>
        <legend>Allowed uses</legend>
        <p>Choose at least one purpose. These permissions do not verify the story text.</p>
        {CAREER_STORY_ALLOWED_USES.map(use => <label key={use.value}>
          <input
            type="checkbox"
            aria-label={`Allow use: ${use.label}`}
            checked={draft.allowedUses.includes(use.value)}
            onChange={event => updateAllowedUse(use.value, event.target.checked)}
          />
          {use.label}
        </label>)}
      </fieldset>

      {!canSaveDraft && <p role="status">Select at least one allowed use and one currently verified, user-approved fact before saving.</p>}

      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={saving || !canSaveDraft}>{saving ? 'Saving revision…' : 'Save as new revision'}</button>
    </form>

    {selectedStory && <section aria-label="Story revision history">
      <h3>Revision history: {latestRevision(selectedStory)?.title}</h3>
      {[...selectedStory.revisions].sort((a, b) => a.revision - b.revision).map(revision => {
        const linkedFacts = revision.factIds.map(id => approvedFacts.find(fact => fact.factId === id));
        const reusable = isStoryRevisionReusable(revision, approvedFacts);
        return <article key={revision.revisionId} aria-label={`Revision ${revision.revision}`}>
          <h4>Revision {revision.revision}: {revision.title}</h4>
          <p><strong>STORY REVIEW STATUS: {revision.reviewStatus}</strong> · <strong>{reusable ? 'AVAILABLE FOR REUSE' : 'UNAVAILABLE FOR REUSE'}</strong></p>
          {revision.reviewedAt && !reusable && <p role="status">This revision was reviewed, but it cannot be reused unless it is current, has an allowed use, and links only current verified, user-approved facts.</p>}
          <p>Saved {revision.createdAt}</p>
          <dl>
            <dt>Situation</dt><dd>{revision.situation}</dd>
            <dt>Task</dt><dd>{revision.task}</dd>
            <dt>Action</dt><dd>{revision.action}</dd>
            <dt>Result</dt><dd>{revision.result}</dd>
          </dl>
          <h5>Linked career facts and provenance</h5>
          {linkedFacts.length
            ? <ul>{linkedFacts.map((fact, index) => <li key={revision.factIds[index]}>
              {fact && <>
                <strong>{fact.verified ? 'FACT VERIFIED' : 'FACT NOT VERIFIED'}</strong> · <strong>{fact.userApproved ? 'FACT USER-APPROVED' : 'FACT NOT USER-APPROVED'}</strong>
                {!fact.verified && ' · This fact is not verified; its linked story text is not established as true.'}
                {fact.verified && !fact.userApproved && ' · This fact has not been approved for reuse.'}
                {revision.staleFactIds?.includes(revision.factIds[index]) && ' · This fact changed after the saved revision; its link is stale.'}
                {' · '}{fact.kind}: {fact.text}<br /><small>Provenance: {fact.source}</small>
                {revision.factFingerprints?.find(entry => entry.factId === revision.factIds[index]) && <small>Linked fact fingerprint: {revision.factFingerprints.find(entry => entry.factId === revision.factIds[index])?.fingerprint}</small>}
              </>}
              {!fact && <><strong>FACT NOT CURRENTLY AVAILABLE</strong> · {revision.factIds[index]} is missing from approved career facts. Its story text is not established as true.</>}
            </li>)}</ul>
            : <p>No approved career facts are linked to this revision.</p>}
          {revision.unstructuredText && <p><strong>Legacy unstructured story text (not parsed as STAR fields):</strong> {revision.unstructuredText}</p>}
          <p><strong>Allowed uses:</strong> {revision.allowedUses.length ? revision.allowedUses.map(use => CAREER_STORY_ALLOWED_USES.find(option => option.value === use)?.label || 'Unknown use').join(', ') : 'None selected'}</p>
          {revision.provenance?.length ? <p><strong>Story provenance references:</strong> {revision.provenance.join('; ')}</p> : null}
          {!revision.reviewedAt && <>
            <label>
              <input
                type="checkbox"
                aria-label={`I reviewed Revision ${revision.revision} and confirm it is accurate`}
                checked={reviewConfirmations[revision.revisionId] || false}
                onChange={event => setReviewConfirmations(previous => ({ ...previous, [revision.revisionId]: event.target.checked }))}
              />
              I reviewed this exact revision and confirm it is accurate.
            </label>
            <button
              type="button"
              disabled={!reviewConfirmations[revision.revisionId] || reviewingRevisionId === revision.revisionId}
              onClick={() => void reviewRevision(selectedStory, revision)}
            >{reviewingRevisionId === revision.revisionId ? 'Reviewing revision…' : 'Review this revision'}</button>
          </>}
          <button type="button" onClick={() => startRevision(selectedStory, revision)}>Edit Revision {revision.revision} as new revision</button>
        </article>;
      })}
    </section>}
  </section>;
}
