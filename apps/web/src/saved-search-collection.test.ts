import { describe, expect, it } from 'vitest';
import { enabledSourcesForSearch, sourceForSearchCollection, withSavedSearchCollectionScope } from './saved-search-collection';

const sources = [
  { id: 'greenhouse', name: 'Greenhouse', enabled: true },
  { id: 'lever', name: 'Lever', enabled: true },
  { id: 'remoteok', name: 'RemoteOK', enabled: false },
];

const search = {
  profile_id: 'profile-1',
  enabled: true,
  source_ids: [],
};

describe('saved-search collection sources', () => {
  it('uses all enabled sources for a search without source restrictions', () => {
    expect(enabledSourcesForSearch(search, sources).map(source => source.id))
      .toEqual(['greenhouse', 'lever']);
  });

  it('restricts collection to enabled sources allowed by the saved search', () => {
    expect(enabledSourcesForSearch({ ...search, source_ids: ['lever', 'remoteok'] }, sources))
      .toEqual([sources[1]]);
  });

  it('requires explicit selection when multiple sources apply', () => {
    expect(sourceForSearchCollection(search, sources, 'profile-1', '')).toBeUndefined();
    expect(sourceForSearchCollection(search, sources, 'profile-1', 'lever')).toBe(sources[1]);
  });

  it('uses the only allowed enabled source without an extra choice', () => {
    expect(sourceForSearchCollection(
      { ...search, source_ids: ['greenhouse'] }, sources, 'profile-1', '',
    )).toBe(sources[0]);
  });

  it('rejects paused searches and searches owned by another profile', () => {
    expect(sourceForSearchCollection({ ...search, enabled: false }, sources, 'profile-1', 'greenhouse'))
      .toBeUndefined();
    expect(sourceForSearchCollection(search, sources, 'profile-2', 'greenhouse')).toBeUndefined();
  });

  it('sends collection under the selected career profile and exact saved search', () => {
    expect(withSavedSearchCollectionScope({ profile_id: 'profile-1', request_id: 'request-1' }, 'search-7'))
      .toEqual({ profile_id: 'profile-1', request_id: 'request-1', search_profile_id: 'search-7' });
  });
});
