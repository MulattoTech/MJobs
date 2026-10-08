import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  SearchProfileSuggestions,
  type SearchProfileSuggestionsProps,
} from './search-profile-suggestions';

function findButtons(node: ReactNode): ReactElement<{
  children?: ReactNode;
  onClick?: () => void;
  'aria-label'?: string;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{
      children?: ReactNode;
      onClick?: () => void;
      'aria-label'?: string;
    }>(child)) return [];
    const current = child.type === 'button' ? [child] : [];
    return [...current, ...findButtons(child.props.children)];
  });
}

const baseProps: SearchProfileSuggestionsProps = {
  profiles: [{ id: 'profile-1', name: 'Infrastructure roles' }],
  selectedProfileId: 'profile-1',
  onSelectProfile: () => {},
  suggestions: [],
  onAddTitle: () => {},
};

describe('search profile suggestion recovery states', () => {
  it('shows a loading status and preserves the selected search profile without changing it', () => {
    const onSelectProfile = vi.fn();
    const onAddTitle = vi.fn();
    const markup = renderToStaticMarkup(
      <SearchProfileSuggestions
        {...baseProps}
        loading
        onSelectProfile={onSelectProfile}
        onAddTitle={onAddTitle}
      />,
    );

    expect(markup).toContain('role="status"');
    expect(markup).toContain('Loading related title suggestions');
    expect(markup).toContain('value="profile-1" selected=""');
    expect(markup).not.toContain('No related title suggestions yet.');
    expect(onSelectProfile).not.toHaveBeenCalled();
    expect(onAddTitle).not.toHaveBeenCalled();
  });

  it('shows a recoverable error and retries without changing the selected profile or adding criteria', () => {
    const onRetry = vi.fn();
    const onSelectProfile = vi.fn();
    const onAddTitle = vi.fn();
    const props = {
      ...baseProps,
      error: 'Could not load title suggestions. Try again.',
      onRetry,
      onSelectProfile,
      onAddTitle,
    };
    const tree = SearchProfileSuggestions(props);
    const markup = renderToStaticMarkup(<SearchProfileSuggestions {...props} />);

    expect(markup).toContain('role="alert"');
    expect(markup).toContain('Could not load title suggestions. Try again.');
    expect(markup).toContain('aria-label="Retry suggestions"');
    expect(markup).toContain('value="profile-1" selected=""');
    expect(onRetry).not.toHaveBeenCalled();
    expect(onSelectProfile).not.toHaveBeenCalled();
    expect(onAddTitle).not.toHaveBeenCalled();

    const retryButton = findButtons(tree).find(button => button.props['aria-label'] === 'Retry suggestions');
    expect(retryButton?.props.onClick).toBeDefined();
    retryButton?.props.onClick?.();

    expect(onRetry).toHaveBeenCalledExactlyOnceWith();
    expect(onSelectProfile).not.toHaveBeenCalled();
    expect(onAddTitle).not.toHaveBeenCalled();
  });
});
