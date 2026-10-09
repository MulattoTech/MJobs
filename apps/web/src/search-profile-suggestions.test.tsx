import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SearchProfileSuggestions, type SearchProfileSuggestionsProps } from './search-profile-suggestions';

function findElements(node: ReactNode, typeName: string): ReactElement<{ children?: ReactNode; onClick?: () => void | Promise<void>; onChange?: (event: { target: { value: string } }) => void; disabled?: boolean }>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{ children?: ReactNode; onClick?: () => void | Promise<void>; onChange?: (event: { target: { value: string } }) => void; disabled?: boolean }>(child)) return [];
    const current = child.type === typeName ? [child] : [];
    return [...current, ...findElements(child.props.children, typeName)];
  });
}

function createStatefulRenderer(componentProps: SearchProfileSuggestionsProps) {
  let addStates: Record<string, { state: string }> = {};
  const useState = vi.spyOn(React, 'useState');
  const ref = { current: new Set<string>() };
  const useRef = vi.spyOn(React, 'useRef');
  useState.mockImplementation(((initialState: unknown) => [
    addStates ?? initialState,
    (nextState: unknown) => {
      addStates = (typeof nextState === 'function'
        ? (nextState as (current: Record<string, { state: string }>) => Record<string, { state: string }>)(addStates)
        : nextState) as Record<string, { state: string }>;
    },
  ]) as typeof React.useState);
  useRef.mockImplementation((() => ref) as typeof React.useRef);

  return {
    render: () => SearchProfileSuggestions(componentProps),
    restore: () => {
      useState.mockRestore();
      useRef.mockRestore();
    },
  };
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
      suggestions: [{ title: 'Platform Administrator', reason, strong_match_count: 4 }],
      onAddTitle,
    };
    const html = renderToStaticMarkup(<SearchProfileSuggestions {...componentProps} />);

    expect(html).toContain('<ul aria-label="Related title suggestions">');
    expect(html).toContain('<p aria-label="Strong-match evidence">Observed in 4 current strong matches.</p>');
    expect(html).toContain(`<p>${reason}</p>`);
    expect(html).toContain('aria-label="Add Platform Administrator to Infrastructure roles"');
    expect(html).toContain('>Add Platform Administrator to Infrastructure roles</button>');
    expect(onAddTitle).not.toHaveBeenCalled();
  });

  it('explains each suggestion and adds only the deliberately selected title to the selected profile', () => {
    const onAddTitle = vi.fn();
    const componentProps = { ...props, onAddTitle };
    const renderer = createStatefulRenderer(componentProps);
    const tree = renderer.render();
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Platform Administrator');
    expect(html).toContain('Appeared in 4 strong matches for System Administrator.');
    expect(html).toContain('>Add Platform Administrator to Infrastructure roles</button>');
    expect(onAddTitle).not.toHaveBeenCalled();

    const addButton = findElements(tree, 'button')[0];
    expect(addButton.props.onClick).toBeDefined();
    addButton.props.onClick?.();
    expect(onAddTitle).toHaveBeenCalledExactlyOnceWith('profile-1', 'Platform Administrator');
    renderer.restore();
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
    const renderer = createStatefulRenderer({ ...props, onSelectProfile });
    const tree = renderer.render();
    const profileSelect = findElements(tree, 'select')[0];

    profileSelect.props.onChange?.({ target: { value: 'profile-1' } });
    expect(onSelectProfile).toHaveBeenCalledExactlyOnceWith('profile-1');
    renderer.restore();
  });

  it('disables duplicate adds while waiting for durable confirmation and announces success only after resolution', async () => {
    let resolveSave!: () => void;
    const save = new Promise<void>(resolve => { resolveSave = resolve; });
    const onAddTitle = vi.fn(() => save);
    const renderer = createStatefulRenderer({ ...props, onAddTitle });

    try {
      const firstButton = findElements(renderer.render(), 'button')[0];
      const firstClick = firstButton.props.onClick?.();
      const pendingTree = renderer.render();
      const pendingButton = findElements(pendingTree, 'button')[0];

      expect(pendingButton.props.disabled).toBe(true);
      expect(renderToStaticMarkup(pendingTree)).toContain('Saving and confirming…');
      expect(renderToStaticMarkup(pendingTree)).not.toContain('was saved to Infrastructure roles');
      await pendingButton.props.onClick?.();
      expect(onAddTitle).toHaveBeenCalledExactlyOnceWith('profile-1', 'Platform Administrator');

      resolveSave();
      await firstClick;
      const successTree = renderer.render();
      expect(renderToStaticMarkup(successTree)).toContain('Platform Administrator was saved to Infrastructure roles.');
      expect(findElements(successTree, 'button')[0].props.disabled).toBe(true);
    } finally {
      renderer.restore();
    }
  });

  it('shows a retryable error when durable save or readback rejects', async () => {
    const onAddTitle = vi.fn().mockRejectedValue(new Error('private backend detail'));
    const renderer = createStatefulRenderer({ ...props, onAddTitle });

    try {
      const initialButton = findElements(renderer.render(), 'button')[0];
      await initialButton.props.onClick?.();
      const errorTree = renderer.render();
      const html = renderToStaticMarkup(errorTree);

      expect(html).toContain('role="alert"');
      expect(html).toContain('Could not save and confirm this title. You can try again.');
      expect(html).not.toContain('private backend detail');
      expect(findElements(errorTree, 'button')[0].props.disabled).toBe(false);
    } finally {
      renderer.restore();
    }
  });

  it('does not claim durable success when a legacy void callback provides no confirmation', async () => {
    const onAddTitle = vi.fn();
    const renderer = createStatefulRenderer({ ...props, onAddTitle });

    try {
      const initialButton = findElements(renderer.render(), 'button')[0];
      await initialButton.props.onClick?.();
      const resultTree = renderer.render();
      const html = renderToStaticMarkup(resultTree);

      expect(html).toContain('Save completion could not be confirmed. Refresh suggestions before trying again.');
      expect(html).not.toContain('was saved to Infrastructure roles');
      expect(findElements(resultTree, 'button')[0].props.disabled).toBe(true);
      expect(onAddTitle).toHaveBeenCalledExactlyOnceWith('profile-1', 'Platform Administrator');
    } finally {
      renderer.restore();
    }
  });
});
