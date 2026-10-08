import React from 'react';

export type SearchProfileSuggestionProfile = {
  id: string;
  name: string;
};

export type SearchProfileTitleSuggestion = {
  title: string;
  reason: string;
};

export type SearchProfileSuggestionsProps = {
  profiles: SearchProfileSuggestionProfile[];
  selectedProfileId: string;
  onSelectProfile: (profileId: string) => void;
  suggestions: SearchProfileTitleSuggestion[];
  onAddTitle: (profileId: string, title: string) => void;
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
            {suggestions.map(suggestion => <li key={suggestion.title}>
              <strong>{suggestion.title}</strong>
              <p>{suggestion.reason}</p>
              <button
                type="button"
                aria-label={`Add ${suggestion.title} to ${selectedProfile?.name ?? 'selected search profile'}`}
                disabled={!selectedProfile}
                onClick={() => {
                  if (selectedProfile) onAddTitle(selectedProfile.id, suggestion.title);
                }}
              >
                Add to selected search profile
              </button>
            </li>)}
          </ul>}
    {!selectedProfile && <p className="muted">Choose a search profile before adding a suggested title.</p>}
  </section>;
}
