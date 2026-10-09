import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DEFAULT_TECHNOLOGY_SKILLS, TechnologySkillSeed } from './technology-skill-seed';

type ButtonProps = { children?: ReactNode; type?: string; onClick?: () => void; disabled?: boolean };
type ElementProps = { children?: ReactNode; type?: string; checked?: boolean; disabled?: boolean; onClick?: () => void; onChange?: (event: {currentTarget: {checked: boolean}}) => void };

function findElements(node: ReactNode, typeName: string): ReactElement<ElementProps>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<ElementProps>(child)) return [];
    return [...(child.type === typeName ? [child] : []), ...findElements(child.props.children, typeName)];
  });
}

describe('optional technology skill seed', () => {
  it('renders every documented skill as an opt-in preference, not a career fact', () => {
    const tree = TechnologySkillSeed({ values: [], onChange: vi.fn() });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Optional technology preferences');
    expect(html).toContain('not verified career facts');
    expect(html).toContain('do not hide jobs without these skills');
    expect(DEFAULT_TECHNOLOGY_SKILLS.map(skill => skill.label)).toEqual([
      'Active Directory', 'Windows Server', 'PowerShell', 'Microsoft 365', 'Entra ID',
      'Azure', 'Group Policy', 'VMware', 'Hyper-V', 'DNS', 'DHCP', 'Intune', 'SCCM',
      'Rubrik', 'SAN', 'NAS',
    ]);
    expect(findElements(tree, 'input')).toHaveLength(16);
    expect(findElements(tree, 'input').every(input => !input.props.checked)).toBe(true);
  });

  it('loads the defaults into the draft only and can clear them', () => {
    const onChange = vi.fn();
    const tree = TechnologySkillSeed({ values: [], onChange });
    const [seed, clear] = findElements(tree, 'button');

    seed.props.onClick?.();
    expect(onChange).toHaveBeenCalledExactlyOnceWith(
      DEFAULT_TECHNOLOGY_SKILLS.map(skill => skill.label));
    clear.props.onClick?.();
    expect(onChange).toHaveBeenNthCalledWith(2, []);
  });

  it('keeps explicitly selected preferences and respects the busy gate', () => {
    const onChange = vi.fn();
    const tree = TechnologySkillSeed({
      values: ['Active Directory', 'PowerShell'], disabled: true, onChange,
    });
    const [seed, clear] = findElements(tree, 'button');
    const checks = findElements(tree, 'input');

    expect(checks[0].props.checked).toBe(true);
    expect(checks[2].props.checked).toBe(true);
    expect(checks[1].props.checked).toBe(false);
    expect(seed.props.disabled).toBe(true);
    expect(clear.props.disabled).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('allows changing an individual opt-in preference', () => {
    const onChange = vi.fn();
    const tree = TechnologySkillSeed({ values: ['Active Directory'], onChange });
    const checks = findElements(tree, 'input');

    checks[0].props.onChange?.({ currentTarget: { checked: false } });
    expect(onChange).toHaveBeenCalledExactlyOnceWith([]);
    checks[1].props.onChange?.({ currentTarget: { checked: true } });
    expect(onChange).toHaveBeenNthCalledWith(2, ['Active Directory', 'Windows Server']);
  });
});
