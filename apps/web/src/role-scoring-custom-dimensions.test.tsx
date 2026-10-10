import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ROLE_SCORING_DIMENSIONS,
  RoleScoringProfileEditorView,
  addCustomScoringDimension,
  createRoleScoringProfileDraft,
  isStableCustomDimensionKey,
  toggleRoleScoringSignal,
  updateRoleScoringThreshold,
  updateRoleScoringWeight,
  loadRoleScoringProfile,
  saveAndReadbackRoleScoringProfile,
  validateRoleScoringProfileDraft,
  type RoleScoringProfileDraft,
  type RoleScoringProfileEditorViewProps,
  type SavedRoleScoringProfile,
} from './role-scoring-profile-editor';

const customDimension = 'organization_transparency';
const customProfile = {
  id: 'custom-scoring-profile',
  version: 'custom-v1',
  schema_version: 1 as const,
  role_family: 'Infrastructure Engineering',
  weights: { skills: 70, [customDimension]: 30 },
  thresholds: { skills: 60, [customDimension]: 45 },
} as SavedRoleScoringProfile;
const customDraft: RoleScoringProfileDraft = {
  schema_version: 1,
  role_family: customProfile.role_family,
  weights: { ...customProfile.weights },
  thresholds: { ...customProfile.thresholds },
};

function findElement(
  node: React.ReactNode,
  predicate: (element: React.ReactElement<any>) => boolean,
): React.ReactElement<any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<any>;
  if (predicate(element)) return element;
  return findElement(element.props.children, predicate);
}

function viewProps(
  overrides: Partial<RoleScoringProfileEditorViewProps> = {},
): RoleScoringProfileEditorViewProps {
  return {
    profileId: 'profile-a',
    searchProfileId: 'search-a',
    profile: customProfile,
    draft: customDraft,
    state: 'ready',
    error: null,
    savedNotice: null,
    onDraftChange: vi.fn(),
    onSave: vi.fn(),
    onRetryLoad: vi.fn(),
    onRetrySave: vi.fn(),
    onReloadLatest: vi.fn(),
    ...overrides,
  };
}

describe('custom scoring dimensions', () => {
  it('keeps built-in dimensions and accepts stable custom keys in saved drafts', () => {
    expect(ROLE_SCORING_DIMENSIONS).toEqual([
      'skills', 'title', 'experience', 'salary', 'work_arrangement', 'employment_type',
      'certification', 'education', 'ai_intensity', 'remote_location',
    ]);
    expect(validateRoleScoringProfileDraft(customDraft)).toEqual([]);
    expect(validateRoleScoringProfileDraft({
      ...customDraft,
      weights: { ...customDraft.weights, 'not-snake-case': 10 },
    })).toContain('weights contains invalid custom dimension key not-snake-case.');
    expect(isStableCustomDimensionKey('x')).toBe(true);
    expect(isStableCustomDimensionKey('invalid_key_'.padEnd(65, 'x'))).toBe(false);
  });

  it('adds a valid custom signal disabled by default and rejects duplicates or built-ins', () => {
    const added = addCustomScoringDimension(createRoleScoringProfileDraft(), customDimension);
    expect(added.weights[customDimension]).toBe(0);
    expect(added.weights.ai_intensity).toBe(0);
    expect(() => addCustomScoringDimension(added, customDimension)).toThrow();
    expect(() => addCustomScoringDimension(added, 'skills')).toThrow();
    expect(() => addCustomScoringDimension(added, 'has-hyphen')).toThrow();
    const enabled = toggleRoleScoringSignal(added, customDimension, true);
    const weighted = updateRoleScoringWeight(enabled, customDimension, 35);
    const thresholded = updateRoleScoringThreshold(weighted, customDimension, 55);
    expect(thresholded.weights[customDimension]).toBe(35);
    expect(thresholded.thresholds[customDimension]).toBe(55);
  });

  it('loads and preserves future signal keys without schema loss', async () => {
    await expect(loadRoleScoringProfile(
      vi.fn(async () => customProfile), 'profile-a', 'search-a',
    )).resolves.toMatchObject({
      weights: { skills: 70, organization_transparency: 30, ai_intensity: 0 },
      thresholds: { skills: 60, organization_transparency: 45 },
    });
    expect(createRoleScoringProfileDraft(customProfile)).toMatchObject({
      weights: { organization_transparency: 30 },
      thresholds: { organization_transparency: 45 },
    });
    const saved = { ...customProfile, version: 'custom-v2' };
    await expect(saveAndReadbackRoleScoringProfile(
      vi.fn(async () => saved),
      vi.fn(async () => saved),
      'profile-a',
      'search-a',
      customDraft,
      'custom-v1',
    )).resolves.toMatchObject({
      weights: { organization_transparency: 30 },
      thresholds: { organization_transparency: 45 },
    });
  });

  it('rejects readback that drops or changes a custom dimension', async () => {
    const saved = { ...customProfile, version: 'custom-v2' };
    const incompleteReadback = {
      ...saved,
      weights: { skills: 70 },
      thresholds: { skills: 60 },
    } as SavedRoleScoringProfile;
    await expect(saveAndReadbackRoleScoringProfile(
      vi.fn(async () => saved),
      vi.fn(async () => incompleteReadback),
      'profile-a',
      'search-a',
      customDraft,
      'custom-v1',
    )).rejects.toMatchObject({ kind: 'readback_mismatch' });
  });

  it('shows custom key, weight, and threshold controls with fail-closed source guidance', () => {
    const html = renderToStaticMarkup(<RoleScoringProfileEditorView {...viewProps()} />);
    expect(html).toContain('Custom scoring signal key');
    expect(html).toContain('Add custom scoring signal');
    expect(html).toContain('organization_transparency weight');
    expect(html).toContain('organization_transparency soft threshold');
    expect(html).toContain('Absent explicit source metadata stays UNKNOWN');
    expect(html).toContain('Custom signals are never inferred from posting prose');
    expect(html).toContain('do not change hard eligibility');
  });

  it('adds a custom key only after an explicit user action', () => {
    const onDraftChange = vi.fn();
    const onCustomDimensionKeyChange = vi.fn();
    const onAddCustomDimension = vi.fn();
    const onSave = vi.fn();
    const tree = RoleScoringProfileEditorView(viewProps({
      onDraftChange,
      onSave,
      customDimensionKey: '',
      customDimensionError: null,
      onCustomDimensionKeyChange,
      onAddCustomDimension,
    } as Partial<RoleScoringProfileEditorViewProps>));
    findElement(tree, element => element.props['aria-label'] === 'Custom scoring signal key')
      ?.props.onChange({ currentTarget: { value: 'organization_transparency' } });
    const enteredKey = RoleScoringProfileEditorView(viewProps({
      onDraftChange,
      onSave,
      customDimensionKey: 'organization_transparency',
      customDimensionError: null,
      onCustomDimensionKeyChange,
      onAddCustomDimension,
    }));
    findElement(enteredKey, element => element.props['aria-label'] === 'Add custom scoring signal')
      ?.props.onClick();
    expect(onCustomDimensionKeyChange).toHaveBeenCalledWith('organization_transparency');
    expect(onAddCustomDimension).toHaveBeenCalledTimes(1);
    expect(onDraftChange).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('wires explicit enable, weight, and soft-threshold edits for saved custom keys', () => {
    const onDraftChange = vi.fn();
    const tree = RoleScoringProfileEditorView(viewProps({ onDraftChange }));
    findElement(tree, element => element.props['aria-label'] === 'Enable organization_transparency custom signal')
      ?.props.onChange({ currentTarget: { checked: true } });
    findElement(tree, element => element.props['aria-label'] === 'organization_transparency weight')
      ?.props.onChange({ currentTarget: { value: '38' } });
    findElement(tree, element => element.props['aria-label'] === 'organization_transparency soft threshold')
      ?.props.onChange({ currentTarget: { value: '62' } });
    expect(onDraftChange).toHaveBeenNthCalledWith(
      1, { ...customDraft, weights: { ...customDraft.weights, [customDimension]: 30 } },
    );
    expect(onDraftChange).toHaveBeenNthCalledWith(
      2, { ...customDraft, weights: { ...customDraft.weights, [customDimension]: 38 } },
    );
    expect(onDraftChange).toHaveBeenNthCalledWith(
      3, { ...customDraft, thresholds: { ...customDraft.thresholds, [customDimension]: 62 } },
    );
  });
});
