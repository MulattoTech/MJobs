import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { REMOTE_INFRASTRUCTURE_SEED, SearchProfileSeed, TEXOMA_LOCAL_IT_SEED } from './search-profile-seed';

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
    expect(html).toContain('Use Remote IT / Infrastructure');
    expect(html).toContain('Use Texoma Local IT / Support');
    expect(onApply).not.toHaveBeenCalled();

    const [button] = findButtons(tree);
    expect(button.props.type).toBe('button');
    button.props.onClick?.();
    expect(onApply).toHaveBeenCalledExactlyOnceWith(REMOTE_INFRASTRUCTURE_SEED);
    expect(onApply.mock.calls[0][0].includeTitles).toHaveLength(18);
  });

  it('disables loading the example while the search form is busy', () => {
    const tree = SearchProfileSeed({ disabled: true, onApply: vi.fn() });
    expect(findButtons(tree).map(button=>button.props.disabled)).toEqual([true,true]);
  });

  it('seeds separate remote and Texoma searches with only their geographic defaults', () => {
    expect(REMOTE_INFRASTRUCTURE_SEED).toMatchObject({
      name: 'Remote IT / Infrastructure',
      remote: 'REQUIRED',
      remoteResidenceState: 'OK',
      workArrangements: ['REMOTE'],
      employmentType: '',
    });
    expect('states' in REMOTE_INFRASTRUCTURE_SEED).toBe(false);
    expect('cities' in REMOTE_INFRASTRUCTURE_SEED).toBe(false);
    expect(TEXOMA_LOCAL_IT_SEED).toMatchObject({
      name: 'Texoma Local IT / Support',
      remote: 'ANY',
      remoteResidenceState: null,
      states: ['OK', 'TX'],
      cities: ['Durant', 'Denison', 'Sherman'],
      workArrangements: ['HYBRID', 'ONSITE'],
      employmentType: '',
    });
    expect(REMOTE_INFRASTRUCTURE_SEED.includeTitles).toContain('Infrastructure Manager');
    expect(REMOTE_INFRASTRUCTURE_SEED.includeTitles).toContain('Infrastructure Operations Engineer');
    expect(REMOTE_INFRASTRUCTURE_SEED.includeTitles).toEqual(expect.arrayContaining([
      'IT Support',
      'Cloud Engineer',
      'DevOps Engineer',
      'Site Reliability Engineer',
      'Cybersecurity Analyst',
      'Security Engineer',
    ]));
    expect(TEXOMA_LOCAL_IT_SEED.includeTitles).toContain('IT Support Technician');
  });

  it('applies the local preset independently and copies its editable arrays', () => {
    const onApply = vi.fn();
    const tree = SearchProfileSeed({ onApply });
    const [, localButton] = findButtons(tree);
    localButton.props.onClick?.();

    expect(onApply).toHaveBeenCalledExactlyOnceWith(TEXOMA_LOCAL_IT_SEED);
    expect(onApply.mock.calls[0][0].cities).not.toBe(TEXOMA_LOCAL_IT_SEED.cities);
    expect(onApply.mock.calls[0][0].states).not.toBe(TEXOMA_LOCAL_IT_SEED.states);
  });
});
