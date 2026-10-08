import React from 'react';

export type RoleLensOption = {
  id: string;
  version: number;
  display_name: string;
  description: string;
};

export type RoleLensEvidenceReference = {
  fact_id: string;
  fingerprint: string;
};

export type RoleLensStoryReference = {
  story_id: string;
  story_key: string;
  revision: number;
  profile_version: string;
  fact_ids: string[];
  fact_fingerprints: RoleLensEvidenceReference[];
};

export type RoleLensSelectionPreview = {
  schema_version: number;
  profile_id: string;
  profile_version: string;
  role_text: string;
  lens: RoleLensOption;
  selection_reason: 'EXPLICIT' | 'ROLE_MATCH' | 'GENERAL_FALLBACK';
  matched_role_terms: string[];
  fact_ids: string[];
  emphasized_fact_ids: string[];
  story_ids: string[];
  emphasized_story_ids: string[];
  source_refs: {
    facts: RoleLensEvidenceReference[];
    stories: RoleLensStoryReference[];
  };
};

/** Exact response contract from GET /profiles/{profile}/jobs/{job}/role-lenses/selection. */
export type RoleLensSelectionApiResponse = {
  schema_version: 1;
  profile_id: string;
  profile_version: string;
  job_id: string;
  job_fingerprint: string;
  role_lenses: RoleLensOption[];
  selection: RoleLensSelectionPreview;
};

export type RoleLensChoice = Pick<RoleLensOption, 'id' | 'version'>;

export type RoleLensPickerProps = {
  profileId: string;
  jobId: string;
  roleLenses: readonly RoleLensOption[];
  /** Deterministic server suggestion; it is shown for context but is never selected automatically. */
  suggestedResponse: RoleLensSelectionApiResponse | null;
  /** Server readback for the user's explicit ID/version request. */
  selectedResponse: RoleLensSelectionApiResponse | null;
  requestedLens: RoleLensChoice | null;
  loading: boolean;
  loadingError: string | null;
  selecting: boolean;
  selectionError: string | null;
  onRetry: () => void;
  onRetrySelection: () => void;
  onSelectLens: (lens: RoleLensChoice | null) => void;
};

function lensChoiceKey(lens: RoleLensChoice): string {
  return `${lens.id}@${lens.version}`;
}

function parseLensChoice(value: string, roleLenses: readonly RoleLensOption[]): RoleLensChoice | null {
  if (!value) return null;
  const option = roleLenses.find(lens => lensChoiceKey(lens) === value);
  return option ? {id: option.id, version: option.version} : null;
}

function readbackConfirms(
  response: RoleLensSelectionApiResponse | null,
  suggestionResponse: RoleLensSelectionApiResponse | null,
  requestedLens: RoleLensChoice | null,
  profileId: string,
  jobId: string,
): RoleLensSelectionPreview | null {
  const preview = response?.selection;
  return !!suggestionResponse
    && response?.profile_id === profileId
    && response.job_id === jobId
    && suggestionResponse.profile_id === profileId
    && suggestionResponse.job_id === jobId
    && response.profile_version === suggestionResponse.profile_version
    && response.job_fingerprint === suggestionResponse.job_fingerprint
    && preview?.profile_id === profileId
    && !!requestedLens
    && preview.selection_reason === 'EXPLICIT'
    && preview.lens.id === requestedLens.id
    && preview.lens.version === requestedLens.version
    ? preview
    : null;
}

export function RoleLensPicker({
  profileId,
  jobId,
  roleLenses,
  suggestedResponse,
  selectedResponse,
  requestedLens,
  loading,
  loadingError,
  selecting,
  selectionError,
  onRetry,
  onRetrySelection,
  onSelectLens,
}: RoleLensPickerProps) {
  const selectedConfirmed = readbackConfirms(selectedResponse, suggestedResponse, requestedLens, profileId, jobId);
  const currentChoice = requestedLens && roleLenses.some(lens => lensChoiceKey(lens) === lensChoiceKey(requestedLens))
    ? lensChoiceKey(requestedLens)
    : '';
  const suggestion = suggestedResponse?.profile_id === profileId
    && suggestedResponse.job_id === jobId
    && suggestedResponse.selection.profile_id === profileId
    ? suggestedResponse.selection.lens
    : null;

  return <section className="panel" aria-label="Role lens selection">
    <h2>Role lens for this job</h2>
    <p>A role lens changes the order or emphasis of existing evidence for a job. It does not create career facts.</p>

    {loading
      ? <p role="status" aria-live="polite">Loading supported role lenses…</p>
      : loadingError
        ? <div role="alert">
          <p>{loadingError}</p>
          <button type="button" onClick={onRetry}>Retry role lens list</button>
        </div>
        : <>
          <label htmlFor="role-lens-picker-select">Role lens for this job</label>
          <select
            id="role-lens-picker-select"
            aria-label="Role lens for this job"
            value={currentChoice}
            disabled={selecting || roleLenses.length === 0}
            onChange={event => onSelectLens(parseLensChoice(event.target.value, roleLenses))}
          >
            <option value="">Choose a role lens version</option>
            {roleLenses.map(lens => <option key={lensChoiceKey(lens)} value={lensChoiceKey(lens)}>
              {lens.display_name} · version {lens.version}
            </option>)}
          </select>

          {!roleLenses.length && <p className="muted">No supported role-lens versions are available.</p>}
          {suggestion && !selectedConfirmed && <div aria-label="Suggested role lens">
            <p>Suggested lens: {suggestion.display_name} (version {suggestion.version}). This suggestion is not selected.</p>
            <p><strong>Suggestion explanation:</strong> {suggestion.description}</p>
          </div>}

          {selecting && <p role="status" aria-live="polite">Confirming the selected lens version with a job-scoped readback…</p>}
          {selectionError && <div role="alert">
            <p>{selectionError}</p>
            {requestedLens && <button type="button" onClick={onRetrySelection}>Retry selected lens readback</button>}
          </div>}

          {selectedConfirmed && <div aria-label="Selected role lens readback">
            <p role="status">Selected lens: {selectedConfirmed.lens.display_name} (version {selectedConfirmed.lens.version}) is confirmed for this job.</p>
            <p><strong>Selected lens explanation:</strong> {selectedConfirmed.lens.description}</p>
            <p><strong>Evidence constraints:</strong> Only current verified, user-approved facts and current approved, fingerprint-matching story revisions are included. The lens can reorder or emphasize evidence, but it cannot create or change facts.</p>
            <h3>Verified facts remain the source evidence</h3>
            {selectedConfirmed.source_refs.facts.length
              ? <ul aria-label="Verified fact evidence">
                {selectedConfirmed.source_refs.facts.map(fact => <li key={fact.fact_id}>
                  Fact {fact.fact_id} · verified evidence fingerprint {fact.fingerprint}
                  {selectedConfirmed.emphasized_fact_ids.includes(fact.fact_id) ? ' · emphasized by this lens' : ''}
                </li>)}
              </ul>
              : <p>No verified, user-approved fact evidence is available in this readback.</p>}
            <h3>Approved story revisions remain linked evidence</h3>
            {selectedConfirmed.source_refs.stories.length
              ? <ul aria-label="Approved story revision evidence">
                {selectedConfirmed.source_refs.stories.map(story => <li key={story.story_id}>
                  Story revision {story.revision} · {story.story_id}
                  {selectedConfirmed.emphasized_story_ids.includes(story.story_id) ? ' · emphasized by this lens' : ''}
                  {story.fact_fingerprints.map(link => <small key={link.fact_id}> · fact {link.fact_id} fingerprint {link.fingerprint}</small>)}
                </li>)}
              </ul>
              : <p>No current approved story revisions are available in this readback.</p>}
          </div>}
        </>}
  </section>;
}
