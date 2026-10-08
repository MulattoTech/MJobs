import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SensitiveAnswerPolicy, type SensitiveAnswerPolicyValue } from './sensitive-answer-policy';

function findElement(node: unknown, predicate: (element: React.ReactElement) => boolean): React.ReactElement | undefined {
  if (!React.isValidElement(node)) return undefined;
  if (predicate(node)) return node;
  const children = (node.props as { children?: React.ReactNode }).children;
  for (const child of React.Children.toArray(children)) {
    const found = findElement(child, predicate);
    if (found) return found;
  }
  return undefined;
}

describe('sensitive answer policy settings', () => {
  it('lists each supported category and describes per-application review and Answer Bank exclusion', () => {
    const html = renderToStaticMarkup(<SensitiveAnswerPolicy value={{}} onValueChange={vi.fn()} />);
    for (const label of ['Race and ethnicity','Religion','Sex and gender','Disability','Veteran status','Other voluntary demographics']) expect(html).toContain(label);
    for (const mode of ['Prefer not to answer','Use configured response','Always require review']) expect(html).toContain(mode);
    expect(html).toContain('Any proposed value requires review for each application');
    expect(html).toContain('never enters reusable Answer Bank suggestions');
  });

  it('shows omitted choices as always requiring review and only shows response input for configured mode', () => {
    const omitted = renderToStaticMarkup(<SensitiveAnswerPolicy value={{}} onValueChange={vi.fn()} />);
    expect(omitted).toContain('value="ALWAYS_REQUIRE_REVIEW" selected=""');
    expect(omitted).not.toContain('aria-label="Race and ethnicity configured response"');

    const configured: SensitiveAnswerPolicyValue = {
      RACE_ETHNICITY: { mode:'USE_CONFIGURED_RESPONSE', response:'User supplied response' },
    };
    const html = renderToStaticMarkup(<SensitiveAnswerPolicy value={configured} onValueChange={vi.fn()} />);
    expect(html).toContain('aria-label="Race and ethnicity configured response"');
    expect(html).toContain('value="User supplied response"');
  });

  it('requires selecting configured response before accepting a response and clears it when switching away', () => {
    const onValueChange = vi.fn();
    const tree = SensitiveAnswerPolicy({ value:{}, onValueChange });
    const modeSelect = findElement(tree, element => element.type === 'select') as React.ReactElement<React.SelectHTMLAttributes<HTMLSelectElement>>;
    const selectProps = modeSelect.props as React.SelectHTMLAttributes<HTMLSelectElement>;
    selectProps.onChange?.({ currentTarget:{ value:'USE_CONFIGURED_RESPONSE' } } as React.ChangeEvent<HTMLSelectElement>);
    expect(onValueChange).toHaveBeenLastCalledWith({ RACE_ETHNICITY:{ mode:'USE_CONFIGURED_RESPONSE' } });

    onValueChange.mockClear();
    const responseTree = SensitiveAnswerPolicy({
      value:{ RACE_ETHNICITY:{ mode:'USE_CONFIGURED_RESPONSE', response:'Prefilled' } },
      onValueChange,
    });
    const responseInput = findElement(responseTree, element => element.type === 'input') as React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>;
    (responseInput.props as React.InputHTMLAttributes<HTMLInputElement>).onChange?.({ currentTarget:{ value:'New response' } } as React.ChangeEvent<HTMLInputElement>);
    expect(onValueChange).toHaveBeenLastCalledWith({ RACE_ETHNICITY:{ mode:'USE_CONFIGURED_RESPONSE', response:'New response' } });

    const configuredTree = SensitiveAnswerPolicy({
      value:{ RACE_ETHNICITY:{ mode:'USE_CONFIGURED_RESPONSE', response:'Should clear' } },
      onValueChange,
    });
    const configuredSelect = findElement(configuredTree, element => element.type === 'select') as React.ReactElement<React.SelectHTMLAttributes<HTMLSelectElement>>;
    (configuredSelect.props as React.SelectHTMLAttributes<HTMLSelectElement>).onChange?.({ currentTarget:{ value:'PREFER_NOT_TO_ANSWER' } } as React.ChangeEvent<HTMLSelectElement>);
    expect(onValueChange).toHaveBeenLastCalledWith({ RACE_ETHNICITY:{ mode:'PREFER_NOT_TO_ANSWER' } });
  });
});
