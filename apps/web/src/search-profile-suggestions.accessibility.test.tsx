import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  SearchProfileSuggestions,
  type SearchProfileSuggestionsProps,
} from './search-profile-suggestions';

function findElements(node: ReactNode, typeName: string): ReactElement<{
  children?: ReactNode;
  onClick?: () => void;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{ children?: ReactNode; onClick?: () => void }>(child)) return [];
    const current = child.type === typeName ? [child] : [];
    return [...current, ...findElements(child.props.children, typeName)];
  });
}

const baseProps: SearchProfileSuggestionsProps = {
  profiles: [{ id: 'profile-1', name: 'Infrastructure roles' }],
  selectedProfileId: 'profile-1',
  onSelectProfile: () => {},
  suggestions: [],
  onAddTitle: () => {},
};

describe('search profile suggestions accessibility', () => {
  it('keeps the empty state inside a named section with an accessible profile control', () => {
    const markup = renderToStaticMarkup(<SearchProfileSuggestions {...baseProps} />);

    expect(markup).toContain('aria-labelledby="search-profile-title-suggestions-heading"');
    expect(markup).toContain('<h3 id="search-profile-title-suggestions-heading">Suggested job titles</h3>');
    expect(markup).toContain('aria-label="Search profile for suggested titles"');
    expect(markup).toContain('No related title suggestions yet.');
    expect(markup).not.toContain('aria-label="Related title suggestions"');
  });

  it('exposes a named suggestion list and add control, and adds only after explicit activation', () => {
    const onAddTitle = vi.fn();
    const props = {
      ...baseProps,
      suggestions: [{ title: 'Platform Administrator', reason: 'Appeared in strong matches.' }],
      onAddTitle,
    };
    const tree = SearchProfileSuggestions(props);
    const markup = renderToStaticMarkup(<SearchProfileSuggestions {...props} />);

    expect(markup).toContain('aria-label="Related title suggestions"');
    expect(markup).toContain('aria-label="Add Platform Administrator to Infrastructure roles"');
    expect(markup).toContain('Appeared in strong matches.');
    expect(onAddTitle).not.toHaveBeenCalled();

    findElements(tree, 'button')[0].props.onClick?.();

    expect(onAddTitle).toHaveBeenCalledExactlyOnceWith('profile-1', 'Platform Administrator');
  });
});
