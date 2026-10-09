import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { REMOTE_INFRASTRUCTURE_SEED, SearchProfileSeed } from './search-profile-seed';

type ButtonProps = { children?: ReactNode; type?: string; onClick?: () => void; disabled?: boolean };

function findButtons(node: ReactNode): ReactElement<ButtonProps>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<ButtonProps>(child)) return [];
    return [...(child.type === 'button' ? [child] : []), ...findButtons(child.props.children)];
  });
}

describe('remote infrastructure search profile seed', () => {
  it('loads the documented example into the editable form without saving or enabling a schedule', () => {
    const onApply = vi.fn();
    const tree = SearchProfileSeed({ onApply });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Search profile example');
    expect(html).toContain('scheduled discovery stays off until you turn it on');
    expect(html).toContain('Use remote IT infrastructure example');
    expect(onApply).not.toHaveBeenCalled();

    const [button] = findButtons(tree);
    expect(button.props.type).toBe('button');
    button.props.onClick?.();
    expect(onApply).toHaveBeenCalledExactlyOnceWith(REMOTE_INFRASTRUCTURE_SEED);
    expect(onApply.mock.calls[0][0].includeTitles).toHaveLength(10);
  });

  it('disables loading the example while the search form is busy', () => {
    const tree = SearchProfileSeed({ disabled: true, onApply: vi.fn() });
    const [button] = findButtons(tree);
    expect(button.props.disabled).toBe(true);
  });
});
