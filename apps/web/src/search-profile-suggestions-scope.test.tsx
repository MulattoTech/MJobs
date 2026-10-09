import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  SearchProfileSuggestions,
  type SearchProfileSuggestionsProps,
} from './search-profile-suggestions';

function findButtons(node: ReactNode): ReactElement<{
  children?: ReactNode;
  onClick?: () => void | Promise<void>;
  disabled?: boolean;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{
      children?: ReactNode;
      onClick?: () => void | Promise<void>;
      disabled?: boolean;
    }>(child)) return [];
    const current = child.type === 'button' ? [child] : [];
    return [...current, ...findButtons(child.props.children)];
  });
}

async function withComponentHooks<T>(run: () => Promise<T>): Promise<T> {
  let state: unknown;
  const ref = { current: new Set<string>() };
  const useState = vi.spyOn(React, 'useState').mockImplementation(((initial: unknown) => [
    state ?? initial,
    (next: unknown) => {
      state = typeof next === 'function'
        ? (next as (value: unknown) => unknown)(state)
        : next;
    },
  ]) as typeof React.useState);
  const useRef = vi.spyOn(React, 'useRef').mockImplementation((() => ref) as typeof React.useRef);
  try {
    return await run();
  } finally {
    useState.mockRestore();
    useRef.mockRestore();
  }
}

describe('search profile suggestion saved-search scope', () => {
  it('passes the selected saved-search ID, preserves criteria, and waits for durable readback', async () => {
    const activeProfileId = 'career-profile-17';
    const selectedSearchId = 'saved-search-82';
    const originalCriteria = {
      include_titles: ['System Administrator'],
      required_keywords: ['Linux'],
      exact_phrases: ['on-call rotation'],
    };
    const savedSearches = [{
      id: selectedSearchId,
      profile_id: activeProfileId,
      criteria: originalCriteria,
    }];
    let persistedCriteria = originalCriteria;
    let confirmReadback!: (criteria: typeof originalCriteria & { include_titles: string[] }) => void;
    const readback = new Promise<typeof originalCriteria & { include_titles: string[] }>(resolve => {
      confirmReadback = resolve;
    });
    const putSearch = vi.fn(async (
      profileId: string,
      searchId: string,
      criteria: typeof originalCriteria & { include_titles: string[] },
    ) => {
      const selected = savedSearches.find(search =>
        search.id === searchId && search.profile_id === profileId,
      );
      if (!selected) throw new Error('Saved search is outside the active profile.');
      persistedCriteria = criteria;
      return criteria;
    });
    const readSearch = vi.fn(async (_profileId: string, _searchId: string) => readback);
    const onAddTitle = vi.fn(async (targetSearchId: string, title: string) => {
      const selected = savedSearches.find(search =>
        search.id === targetSearchId && search.profile_id === activeProfileId,
      );
      if (!selected) throw new Error('Choose a saved search for this profile.');
      const nextCriteria = {
        ...selected.criteria,
        include_titles: [...selected.criteria.include_titles, title],
      };
      const saved = await putSearch(selected.profile_id, selected.id, nextCriteria);
      const confirmed = await readSearch(selected.profile_id, selected.id);
      if (JSON.stringify(saved) !== JSON.stringify(confirmed)) {
        throw new Error('Saved search readback did not match.');
      }
    });
    const props: SearchProfileSuggestionsProps = {
      profiles: [{ id: selectedSearchId, name: 'Systems Administrator search' }],
      selectedProfileId: selectedSearchId,
      onSelectProfile: () => {},
      suggestions: [{
        title: 'Platform Administrator',
        reason: 'Appeared in strong matches for System Administrator.',
      }],
      onAddTitle,
    };

    await withComponentHooks(async () => {
      const tree = SearchProfileSuggestions(props);
      expect(onAddTitle).not.toHaveBeenCalled();
      expect(putSearch).not.toHaveBeenCalled();
      expect(persistedCriteria).toEqual(originalCriteria);

      findButtons(tree)[0].props.onClick?.();
      const durableCompletion = onAddTitle.mock.results[0].value;
      await Promise.resolve();
      expect(onAddTitle).toHaveBeenCalledExactlyOnceWith(selectedSearchId, 'Platform Administrator');
      expect(onAddTitle).not.toHaveBeenCalledWith(activeProfileId, 'Platform Administrator');
      expect(putSearch).toHaveBeenCalledExactlyOnceWith(
        activeProfileId,
        selectedSearchId,
        {
          ...originalCriteria,
          include_titles: ['System Administrator', 'Platform Administrator'],
        },
      );
      expect(readSearch).toHaveBeenCalledExactlyOnceWith(activeProfileId, selectedSearchId);
      const pendingMarkup = renderToStaticMarkup(SearchProfileSuggestions(props));
      expect(persistedCriteria).toEqual({
        ...originalCriteria,
        include_titles: ['System Administrator', 'Platform Administrator'],
      });
      expect(pendingMarkup).toContain('Saving and confirming…');
      expect(pendingMarkup).not.toContain('Platform Administrator was saved');

      confirmReadback({
        ...originalCriteria,
        include_titles: ['System Administrator', 'Platform Administrator'],
      });
      await durableCompletion;
      await Promise.resolve();

      expect(persistedCriteria).toEqual({
        ...originalCriteria,
        include_titles: ['System Administrator', 'Platform Administrator'],
      });
      expect(renderToStaticMarkup(SearchProfileSuggestions(props)))
        .toContain('Platform Administrator was saved to Systems Administrator search.');
    });
  });
});
