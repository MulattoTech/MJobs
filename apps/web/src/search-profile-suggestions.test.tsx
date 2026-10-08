import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SearchProfileSuggestions, type SearchProfileSuggestionsProps } from './search-profile-suggestions';

function findElements(node: ReactNode, typeName: string): ReactElement<{ children?: ReactNode; onClick?: () => void; onChange?: (event: { target: { value: string } }) => void }>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{ children?: ReactNode; onClick?: () => void; onChange?: (event: { target: { value: string } }) => void }>(child)) return [];
    const current = child.type === typeName ? [child] : [];
    return [...current, ...findElements(child.props.children, typeName)];
  });
}

const props: SearchProfileSuggestionsProps = {
  profiles: [{ id: 'profile-1', name: 'Infrastructure roles' }],
  selectedProfileId: 'profile-1',
  onSelectProfile: () => {},
  suggestions: [{ title: 'Platform Administrator', reason: 'Appeared in 4 strong matches for System Administrator.' }],
  onAddTitle: () => {},
};

describe('search profile title suggestions', () => {
  it('renders the current strong-match count explanation accessibly without adding the title', () => {
    const onAddTitle = vi.fn();
    const reason = 'Observed in 4 current strong matches.';
    const componentProps = {
      ...props,
      suggestions: [{ title: 'Platform Administrator', reason }],
      onAddTitle,
    };
    const html = renderToStaticMarkup(<SearchProfileSuggestions {...componentProps} />);

    expect(html).toContain('<ul aria-label="Related title suggestions">');
    expect(html).toContain(`<p>${reason}</p>`);
    expect(html).toContain('aria-label="Add Platform Administrator to Infrastructure roles"');
    expect(onAddTitle).not.toHaveBeenCalled();
  });

  it('explains each suggestion and adds only the deliberately selected title to the selected profile', () => {
    const onAddTitle = vi.fn();
    const componentProps = { ...props, onAddTitle };
    const tree = SearchProfileSuggestions(componentProps);
    const html = renderToStaticMarkup(<SearchProfileSuggestions {...componentProps} />);

    expect(html).toContain('Platform Administrator');
    expect(html).toContain('Appeared in 4 strong matches for System Administrator.');
    expect(html).toContain('Add to selected search profile');
    expect(onAddTitle).not.toHaveBeenCalled();

    const addButton = findElements(tree, 'button')[0];
    expect(addButton.props.onClick).toBeDefined();
    addButton.props.onClick?.();
    expect(onAddTitle).toHaveBeenCalledExactlyOnceWith('profile-1', 'Platform Administrator');
  });

  it('requires selecting a profile before a title can be added', () => {
    const html = renderToStaticMarkup(
      <SearchProfileSuggestions {...props} selectedProfileId="" />,
    );

    expect(html).toContain('Choose a search profile before adding a suggested title.');
    expect(html).toContain('disabled=""');
  });

  it('keeps profile selection under the owning screen callback', () => {
    const onSelectProfile = vi.fn();
    const tree = SearchProfileSuggestions({ ...props, onSelectProfile });
    const profileSelect = findElements(tree, 'select')[0];

    profileSelect.props.onChange?.({ target: { value: 'profile-1' } });
    expect(onSelectProfile).toHaveBeenCalledExactlyOnceWith('profile-1');
  });
});
