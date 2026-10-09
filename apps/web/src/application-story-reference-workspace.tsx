import React, {useEffect, useId, useRef, useState} from 'react';
import {isSensitiveAnswerQuestion} from './answer-matching';

export type ApplicationStoryReferenceScope = {
  profile_id: string;
  application_id: string;
  question: string;
};

export type ApplicationStoryFactProvenance = {
  fact_id: string;
  fingerprint: string;
};

export type CurrentApplicationStoryReference = {
  story_id: string;
  revision: number;
  current_revision: number;
  is_current: true;
  title: string;
  text: string;
  allowed_use: 'APPLICATION_ANSWER';
  review_status: 'APPROVED';
  fact_provenance: ApplicationStoryFactProvenance[];
};

export type ApplicationStoryReferenceResponse = ApplicationStoryReferenceScope & {
  story_references: CurrentApplicationStoryReference[];
};

/** Read-only client for current approved references in one application-question scope. */
export type ApplicationStoryReferenceClient = {
  load: (scope: ApplicationStoryReferenceScope) => Promise<unknown>;
};

/** This payload contains provenance only; story text is never passed as answer content. */
export type ApplicationStoryCitation = {
  profile_id: string;
  application_id: string;
  question: string;
  story_id: string;
  revision: number;
  allowed_use: 'APPLICATION_ANSWER';
  fact_provenance: ApplicationStoryFactProvenance[];
};

export type ApplicationStoryReferenceWorkspaceProps = ApplicationStoryReferenceScope & {
  client: ApplicationStoryReferenceClient;
  onCitationChange: (citation: ApplicationStoryCitation | null) => void;
};

export type ApplicationStoryReferenceValidationCode =
  | 'STALE_SCOPE'
  | 'SENSITIVE_QUESTION'
  | 'INVALID_CURRENTNESS'
  | 'INVALID_PROVENANCE'
  | 'INVALID_REFERENCE';

export type ApplicationStoryReferenceValidation =
  | {ok: true; response: ApplicationStoryReferenceResponse}
  | {ok: false; code: ApplicationStoryReferenceValidationCode};

export type ApplicationStoryReferenceLoadResult =
  | {kind: 'ready'; response: ApplicationStoryReferenceResponse}
  | {kind: 'stale'}
  | {kind: 'sensitive'}
  | {kind: 'invalid'; code: Exclude<ApplicationStoryReferenceValidationCode, 'STALE_SCOPE' | 'SENSITIVE_QUESTION'>}
  | {kind: 'error'};

type ScopedLoadState =
  | {scopeKey: string; kind: 'idle' | 'loading' | 'error' | 'stale' | 'sensitive' | 'invalid'}
  | {scopeKey: string; kind: 'ready'; response: ApplicationStoryReferenceResponse};

type ScopedReviewState = {scopeKey: string; reviewed: boolean};
type ScopedCitationState = {
  scopeKey: string;
  citation: ApplicationStoryCitation;
  title: string;
};

const sameScope = (left: ApplicationStoryReferenceScope, right: ApplicationStoryReferenceScope) =>
  left.profile_id === right.profile_id && left.application_id === right.application_id &&
  left.question === right.question;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const isText = (value: unknown): value is string =>
  typeof value === 'string' && value.trim().length > 0;

function validateReference(value: unknown):
  | {ok: true; reference: CurrentApplicationStoryReference}
  | {ok: false; code: 'INVALID_CURRENTNESS' | 'INVALID_PROVENANCE' | 'INVALID_REFERENCE'} {
  if (!isRecord(value)) return {ok: false, code: 'INVALID_REFERENCE'};
  const revision = value.revision;
  if (value.is_current !== true || !Number.isSafeInteger(revision) ||
      !Number.isSafeInteger(value.current_revision) || revision !== value.current_revision ||
      (revision as number) < 1) {
    return {ok: false, code: 'INVALID_CURRENTNESS'};
  }
  if (!isText(value.story_id) || !isText(value.title) || !isText(value.text) ||
      value.allowed_use !== 'APPLICATION_ANSWER' || value.review_status !== 'APPROVED') {
    return {ok: false, code: 'INVALID_REFERENCE'};
  }
  if (!Array.isArray(value.fact_provenance) || value.fact_provenance.length === 0 ||
      !value.fact_provenance.every(fact => isRecord(fact) && isText(fact.fact_id) &&
        isText(fact.fingerprint)) ||
      new Set(value.fact_provenance.map(fact => (fact as Record<string, unknown>).fact_id)).size !==
        value.fact_provenance.length) {
    return {ok: false, code: 'INVALID_PROVENANCE'};
  }
  return {
    ok: true,
    reference: {
      story_id: value.story_id,
      revision: revision as number,
      current_revision: value.current_revision as number,
      is_current: true,
      title: value.title,
      text: value.text,
      allowed_use: 'APPLICATION_ANSWER',
      review_status: 'APPROVED',
      fact_provenance: value.fact_provenance.map(fact => ({
        fact_id: (fact as Record<string, string>).fact_id,
        fingerprint: (fact as Record<string, string>).fingerprint,
      })),
    },
  };
}

export function validateApplicationStoryReferenceResponse(
  scope: ApplicationStoryReferenceScope,
  value: unknown,
): ApplicationStoryReferenceValidation {
  if (isSensitiveAnswerQuestion(scope.question)) return {ok: false, code: 'SENSITIVE_QUESTION'};
  if (!isRecord(value) || value.profile_id !== scope.profile_id ||
      value.application_id !== scope.application_id || value.question !== scope.question) {
    return {ok: false, code: 'STALE_SCOPE'};
  }
  if (!Array.isArray(value.story_references)) return {ok: false, code: 'INVALID_REFERENCE'};

  const references: CurrentApplicationStoryReference[] = [];
  const identities = new Set<string>();
  for (const rawReference of value.story_references) {
    const validated = validateReference(rawReference);
    if (!validated.ok) return validated;
    const identity = `${validated.reference.story_id}:${validated.reference.revision}`;
    if (identities.has(identity)) return {ok: false, code: 'INVALID_REFERENCE'};
    identities.add(identity);
    references.push(validated.reference);
  }
  return {
    ok: true,
    response: {
      profile_id: scope.profile_id,
      application_id: scope.application_id,
      question: scope.question,
      story_references: references,
    },
  };
}

export async function requestApplicationStoryReferences(
  scope: ApplicationStoryReferenceScope,
  client: ApplicationStoryReferenceClient,
  getActiveScope: () => ApplicationStoryReferenceScope,
): Promise<ApplicationStoryReferenceLoadResult> {
  if (isSensitiveAnswerQuestion(scope.question)) return {kind: 'sensitive'};
  let value: unknown;
  try {
    value = await client.load({...scope});
  } catch {
    return sameScope(scope, getActiveScope()) ? {kind: 'error'} : {kind: 'stale'};
  }
  if (!sameScope(scope, getActiveScope())) return {kind: 'stale'};
  const validation = validateApplicationStoryReferenceResponse(scope, value);
  if (validation.ok) return {kind: 'ready', response: validation.response};
  if (validation.code === 'STALE_SCOPE' || validation.code === 'INVALID_CURRENTNESS') return {kind: 'stale'};
  if (validation.code === 'SENSITIVE_QUESTION') return {kind: 'sensitive'};
  return {kind: 'invalid', code: validation.code};
}

export function createReviewedStoryCitation(
  scope: ApplicationStoryReferenceScope,
  value: unknown,
  reviewed: boolean,
): ApplicationStoryCitation | null {
  if (!reviewed || isSensitiveAnswerQuestion(scope.question)) return null;
  const validated = validateReference(value);
  if (!validated.ok) return null;
  const reference = validated.reference;
  return {
    profile_id: scope.profile_id,
    application_id: scope.application_id,
    question: scope.question,
    story_id: reference.story_id,
    revision: reference.revision,
    allowed_use: 'APPLICATION_ANSWER',
    fact_provenance: reference.fact_provenance.map(fact => ({...fact})),
  };
}

export function ApplicationStoryReferenceWorkspace({
  profile_id,
  application_id,
  question,
  client,
  onCitationChange,
}: ApplicationStoryReferenceWorkspaceProps) {
  const scope: ApplicationStoryReferenceScope = {profile_id, application_id, question};
  const scopeKey = JSON.stringify(scope);
  const headingId = useId();
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const callback = useRef(onCitationChange);
  callback.current = onCitationChange;
  const requestSequence = useRef(0);
  const selectedStateRef = useRef<ScopedCitationState | null>(null);
  const [loadState, setLoadState] = useState<ScopedLoadState>({scopeKey, kind: 'idle'});
  const [reviewState, setReviewState] = useState<ScopedReviewState>({scopeKey, reviewed: false});
  const [selectedState, setSelectedState] = useState<ScopedCitationState | null>(null);
  const sensitive = isSensitiveAnswerQuestion(question);

  useEffect(() => {
    requestSequence.current += 1;
    setLoadState({scopeKey, kind: 'idle'});
    setReviewState({scopeKey, reviewed: false});
    const priorSelection = selectedStateRef.current;
    if (priorSelection && priorSelection.scopeKey !== scopeKey) callback.current(null);
    const currentSelection = priorSelection?.scopeKey === scopeKey ? priorSelection : null;
    selectedStateRef.current = currentSelection;
    setSelectedState(currentSelection);
    return () => { requestSequence.current += 1; };
  }, [scopeKey]);

  const currentLoadState = loadState.scopeKey === scopeKey ? loadState : {scopeKey, kind: 'idle' as const};
  const reviewed = reviewState.scopeKey === scopeKey && reviewState.reviewed;
  const currentSelection = selectedState?.scopeKey === scopeKey ? selectedState : null;

  async function loadReferences() {
    if (sensitive || !profile_id.trim() || !application_id.trim() || !question.trim()) return;
    const requestedScope = {...scope};
    const sequence = ++requestSequence.current;
    setLoadState({scopeKey, kind: 'loading'});
    setReviewState({scopeKey, reviewed: false});
    setSelectedState(null);
    selectedStateRef.current = null;
    callback.current(null);

    const result = await requestApplicationStoryReferences(
      requestedScope,
      client,
      () => activeScope.current,
    );
    if (sequence !== requestSequence.current || !sameScope(requestedScope, activeScope.current)) return;
    if (result.kind === 'ready') {
      setLoadState({scopeKey, kind: 'ready', response: result.response});
    } else if (result.kind === 'invalid') {
      setLoadState({scopeKey, kind: 'invalid'});
    } else {
      setLoadState({scopeKey, kind: result.kind});
    }
  }

  function selectCitation(reference: CurrentApplicationStoryReference) {
    if (!reviewed || currentLoadState.kind !== 'ready' || sensitive ||
        !sameScope(scope, currentLoadState.response)) return;
    const matches = currentLoadState.response.story_references.filter(candidate =>
      candidate.story_id === reference.story_id && candidate.revision === reference.revision);
    if (matches.length !== 1) {
      setLoadState({scopeKey, kind: 'stale'});
      setReviewState({scopeKey, reviewed: false});
      setSelectedState(null);
      selectedStateRef.current = null;
      callback.current(null);
      return;
    }
    const citation = createReviewedStoryCitation(scope, matches[0], reviewed);
    if (!citation) return;
    const selected = {scopeKey, citation, title: matches[0].title};
    setSelectedState(selected);
    selectedStateRef.current = selected;
    callback.current(citation);
  }

  const selectedCitation = currentSelection?.citation;
  return <section className="panel" aria-labelledby={headingId} aria-label="Application story reference workspace">
    <h2 id={headingId}>Application story references</h2>
    <p>Profile {profile_id || 'unavailable'} · application {application_id || 'unavailable'}</p>
    <p>Question: {question || 'No question selected.'}</p>
    <p className="muted">Choose an exact, current, approved story as optional source provenance. A citation will not fill, generate, save, or submit answer text.</p>
    {sensitive
      ? <p role="alert">Sensitive or legally significant questions do not receive career story references.</p>
      : <>
        <button
          type="button"
          aria-label={currentLoadState.kind === 'error' ? 'Retry loading approved story references' : 'Load approved story references'}
          disabled={currentLoadState.kind === 'loading' || !profile_id.trim() || !application_id.trim() || !question.trim()}
          onClick={() => void loadReferences()}
        >
          {currentLoadState.kind === 'loading'
            ? 'Loading approved story references…'
            : currentLoadState.kind === 'error'
              ? 'Retry loading approved story references'
              : 'Load approved story references'}
        </button>
        {currentLoadState.kind === 'loading' && <p role="status" aria-live="polite">Loading current story references for this application question…</p>}
        {currentLoadState.kind === 'error' && <p role="alert">Could not load approved story references. Retry the lookup.</p>}
        {currentLoadState.kind === 'stale' && <p role="alert">Story references were stale or belonged to a different scope. Reload before selecting a citation.</p>}
        {currentLoadState.kind === 'invalid' && <p role="alert">The story service returned incomplete or ineligible provenance. Reload the approved story references.</p>}
        {currentLoadState.kind === 'ready' && currentLoadState.response.story_references.length === 0 &&
          <p>No current approved story references are available for this question.</p>}
        {currentLoadState.kind === 'ready' && currentLoadState.response.story_references.length > 0 && <>
          <label>
            <input
              type="checkbox"
              checked={reviewed}
              onChange={event => {
                setReviewState({scopeKey, reviewed: event.currentTarget.checked});
                if (!event.currentTarget.checked) {
                  setSelectedState(null);
                  selectedStateRef.current = null;
                  callback.current(null);
                }
              }}
            />
            I reviewed these approved story references for this answer
          </label>
          {!reviewed && <p className="muted">Review the story title, exact revision, and linked fact fingerprints before selecting a citation.</p>}
          <fieldset disabled={!reviewed}>
            <legend>Current approved story references</legend>
            <ul aria-label="Current approved story references">
              {currentLoadState.response.story_references.map(reference => {
                const selected = selectedCitation?.story_id === reference.story_id &&
                  selectedCitation.revision === reference.revision;
                return <li key={`${reference.story_id}:${reference.revision}`}>
                  <label>
                    <input
                      type="radio"
                      name={`story-reference-${headingId}`}
                      aria-label={`Select ${reference.title} revision ${reference.revision} as a citation`}
                      checked={selected}
                      disabled={!reviewed}
                      onChange={event => { if (event.currentTarget.checked) selectCitation(reference); }}
                    />
                    <strong>{reference.title}</strong> · revision {reference.revision}
                  </label>
                  <p>Story revision ID: <code>{reference.story_id}</code> · status {reference.review_status} · allowed use Application answer</p>
                  <p>Source story text: {reference.text}</p>
                  <p><strong>Fact provenance</strong></p>
                  <ul>{reference.fact_provenance.map(fact => <li key={fact.fact_id}>
                    Fact {fact.fact_id} · fingerprint {fact.fingerprint}
                  </li>)}</ul>
                </li>;
              })}
            </ul>
          </fieldset>
          {selectedCitation && currentSelection && <section aria-label="Selected story citation">
            <h3>Selected story citation</h3>
            <p>{currentSelection.title} · story revision ID {selectedCitation.story_id} · revision {selectedCitation.revision}</p>
            <p>Fact provenance: {selectedCitation.fact_provenance.map(fact => `${fact.fact_id} (${fact.fingerprint})`).join('; ')}</p>
            <p>This citation contains only story and fact references. Answer text remains unchanged.</p>
            <button type="button" onClick={() => {
              setSelectedState(null);
              selectedStateRef.current = null;
              callback.current(null);
            }}>Clear citation selection</button>
          </section>}
        </>}
      </>}
  </section>;
}
