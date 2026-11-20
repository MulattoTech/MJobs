import React, { type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  CareerFactReviewReload,
  assertProfileDraftUnchanged,
  factReviewMatchesProfileVersion,
  hasUnsavedProfileDraft,
  isFactReviewVersionConflict,
  profileDraftChangedSince,
  profileDraftFingerprint,
} from './career-fact-rejection';

function findButton(node: ReactNode): ReactElement<{onClick: () => void; children: ReactNode}> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as ReactElement<{onClick?: () => void; children: ReactNode}>;
  if (element.type === 'button' && typeof element.props.onClick === 'function') {
    return element as ReactElement<{onClick: () => void; children: ReactNode}>;
  }
  return findButton(element.props.children);
}

describe('career fact review reload', () => {
  it('detects an answer-driven profile version change before fact review', () => {
    const rejectionReadback = {
      profile_id: 'profile-1', profile_version: 'version-2', facts: [],
    };
    expect(factReviewMatchesProfileVersion('profile-1', 'version-1', rejectionReadback)).toBe(false);
    expect(factReviewMatchesProfileVersion('profile-1', 'version-2', rejectionReadback)).toBe(true);
    expect(isFactReviewVersionConflict(Object.assign(new Error('conflict'), {
      status: 409, code: 'FACT_REJECTION_VERSION_CHANGED',
    }))).toBe(true);
    expect(isFactReviewVersionConflict(Object.assign(new Error('other conflict'), {
      status: 409, code: 'FACT_REJECTION_CONTENT_CHANGED',
    }))).toBe(false);
    expect(isFactReviewVersionConflict(Object.assign(new Error('missing'), {status: 404}))).toBe(false);
  });

  it('recognizes an unsaved fact draft so reload requires an explicit discard choice', () => {
    const saved = {id: 'profile-1', version: 'version-1', facts: [
      {id: 'fact-1', text: 'Original fact', verified: false, user_approved: false},
    ]};
    const draft = {...saved, facts: [{...saved.facts[0], text: 'Edited fact'}]};
    expect(hasUnsavedProfileDraft(saved, saved)).toBe(false);
    expect(hasUnsavedProfileDraft(draft, saved)).toBe(true);
    expect(hasUnsavedProfileDraft(draft, null)).toBe(true);
    const capturedDraft = profileDraftFingerprint(saved);
    expect(profileDraftChangedSince(saved, capturedDraft)).toBe(false);
    expect(profileDraftChangedSince(draft, capturedDraft)).toBe(true);
  });

  it('preserves an edit made while the explicit reload read is pending', async () => {
    const saved = {id: 'profile-1', version: 'version-1', facts: [
      {id: 'fact-1', text: 'Original fact', verified: false, user_approved: false},
    ]};
    let currentDraft = saved;
    const fingerprintAtClick = profileDraftFingerprint(currentDraft);
    let finishRead!: () => void;
    const readPending = new Promise<void>(resolve => {finishRead = resolve;});
    const reload = async () => {
      await readPending;
      assertProfileDraftUnchanged(currentDraft, fingerprintAtClick);
    };

    const reloadPending = reload();
    currentDraft = {...saved, facts: [{...saved.facts[0], text: 'New unsaved edit'}]};
    finishRead();

    await expect(reloadPending).rejects.toThrow('No edits were discarded');
    expect(currentDraft.facts[0].text).toBe('New unsaved edit');
  });

  it('requires a visible user action and warns before discarding an unsaved draft', () => {
    const onReload = vi.fn();
    const view = CareerFactReviewReload({stale: true, hasUnsavedDraft: true, onReload, disabled: false});
    const html = renderToStaticMarkup(view);
    expect(html).toContain('Reloading will discard unsaved profile changes.');
    expect(html).toContain('Discard draft and reload');
    expect(onReload).not.toHaveBeenCalled();
    findButton(view)?.props.onClick();
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it('offers a direct reload when the profile editor has no unsaved draft', () => {
    const view = <CareerFactReviewReload stale hasUnsavedDraft={false} onReload={vi.fn()} disabled={false}/>;
    expect(renderToStaticMarkup(view)).toContain('Reload saved facts');
    expect(renderToStaticMarkup(<CareerFactReviewReload stale={false} hasUnsavedDraft={false} onReload={vi.fn()} disabled={false}/>)).toBe('');
  });
});
