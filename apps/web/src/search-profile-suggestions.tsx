import React from 'react';

export type SearchProfileSuggestionProfile = {
  id: string;
  name: string;
};

export type SearchProfileTitleSuggestion = {
  title: string;
  reason: string;
  strong_match_count?: number;
};

export type SearchProfileSuggestionsProps = {
  profiles: SearchProfileSuggestionProfile[];
  selectedProfileId: string;
  onSelectProfile: (profileId: string) => void;
  suggestions: SearchProfileTitleSuggestion[];
  onAddTitle: (profileId: string, title: string) => Promise<void> | void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
};

export function SearchProfileSuggestions({
  profiles,
  selectedProfileId,
  onSelectProfile,
  suggestions,
  onAddTitle,
  loading = false,
  error = null,
  onRetry,
}: SearchProfileSuggestionsProps) {
  type AddState = 'pending' | 'success' | 'error' | 'unconfirmed';
  const [addStates, setAddStates] = React.useState<Record<string, AddState>>({});
  const inFlight = React.useRef(new Set<string>());
  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId);

  return <section aria-labelledby="search-profile-title-suggestions-heading">
    <h3 id="search-profile-title-suggestions-heading">Suggested job titles</h3>
    <p className="muted">
      These titles appeared in strong matches. Review each reason and add titles individually; suggestions never change search criteria on their own.
    </p>
    <label htmlFor="search-profile-suggestions-profile">Search profile for suggested titles</label>
    <select
      id="search-profile-suggestions-profile"
      aria-label="Search profile for suggested titles"
      value={selectedProfile ? selectedProfile.id : ''}
      onChange={event => onSelectProfile(event.target.value)}
    >
      <option value="">Choose a search profile</option>
      {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
    </select>

    {loading
      ? <p className="muted" role="status" aria-live="polite">Loading related title suggestions…</p>
      : error
        ? <div role="alert">
          <p>{error}</p>
          {onRetry && <button type="button" aria-label="Retry suggestions" onClick={onRetry}>
            Retry suggestions
          </button>}
        </div>
        : suggestions.length === 0
          ? <p className="muted">No related title suggestions yet.</p>
          : <ul aria-label="Related title suggestions">
            {suggestions.map(suggestion => {
              const profileId = selectedProfile?.id;
              const targetName = selectedProfile?.name ?? 'selected search profile';
              const key = JSON.stringify([profileId, suggestion.title]);
              const addState = addStates[key];
              const disabled = !selectedProfile || addState === 'pending' || addState === 'success' || addState === 'unconfirmed';
              const addTitle = async () => {
                if (!selectedProfile || disabled || inFlight.current.has(key)) return;

                inFlight.current.add(key);
                setAddStates(current => ({ ...current, [key]: 'pending' }));
                try {
                  const completion = onAddTitle(selectedProfile.id, suggestion.title);
                  if (completion && typeof completion.then === 'function') {
                    await completion;
                    setAddStates(current => ({ ...current, [key]: 'success' }));
                  } else {
                    setAddStates(current => ({ ...current, [key]: 'unconfirmed' }));
                  }
                } catch {
                  setAddStates(current => ({ ...current, [key]: 'error' }));
                } finally {
                  inFlight.current.delete(key);
                }
              };

              return <li key={suggestion.title}>
                <strong>{suggestion.title}</strong>
                {typeof suggestion.strong_match_count === 'number' && Number.isInteger(suggestion.strong_match_count) && suggestion.strong_match_count > 0 && <p aria-label="Strong-match evidence">
                  Observed in {suggestion.strong_match_count} current strong {suggestion.strong_match_count === 1 ? 'match' : 'matches'}.
                </p>}
                <p>{suggestion.reason}</p>
                <button
                  type="button"
                  aria-label={`Add ${suggestion.title} to ${targetName}`}
                  disabled={disabled}
                  onClick={() => { void addTitle(); }}
                >
                  {addState === 'pending' ? 'Saving and confirming…' : `Add ${suggestion.title} to ${targetName}`}
                </button>
                {addState === 'pending' && <p role="status" aria-live="polite">Saving and confirming…</p>}
                {addState === 'success' && <p role="status" aria-live="polite">{suggestion.title} was saved to {targetName}.</p>}
                {addState === 'error' && <p role="alert">Could not save and confirm this title. You can try again.</p>}
                {addState === 'unconfirmed' && <p role="status" aria-live="polite">Save completion could not be confirmed. Refresh suggestions before trying again.</p>}
              </li>;
            })}
          </ul>}
    {!selectedProfile && <p className="muted">Choose a search profile before adding a suggested title.</p>}
  </section>;
}
