export type SearchCollectionScope = {
  profile_id: string;
  enabled: boolean;
  source_ids: string[];
};

export type CollectionSource = {
  id: string;
  name: string;
  enabled: boolean;
};

export function withSavedSearchCollectionScope<
  T extends { profile_id: string; request_id: string },
>(request: T, searchProfileId: string): T & { search_profile_id: string } {
  return { ...request, search_profile_id: searchProfileId };
}

export function enabledSourcesForSearch<S extends CollectionSource>(
  search: SearchCollectionScope,
  sources: S[],
): S[] {
  return sources.filter(source => source.enabled
    && (search.source_ids.length === 0 || search.source_ids.includes(source.id)));
}

export function sourceForSearchCollection<S extends CollectionSource>(
  search: SearchCollectionScope,
  sources: S[],
  activeProfileId: string,
  selectedSourceId: string,
): S | undefined {
  if (!search.enabled || search.profile_id !== activeProfileId) {
    return undefined;
  }

  const available = enabledSourcesForSearch(search, sources);
  if (available.length === 1) {
    return available[0];
  }
  return available.find(source => source.id === selectedSourceId);
}
