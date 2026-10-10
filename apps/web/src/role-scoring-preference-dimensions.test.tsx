import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it, vi} from 'vitest';
import {
  RoleScoringProfileEditorView, createRoleScoringProfileDraft, toggleRoleScoringSignal,
  updateRoleScoringWeight, saveAndReadbackRoleScoringProfile,
} from './role-scoring-profile-editor';

describe('native preference weights',()=>{
  it('explains preference provenance and UNKNOWN-neutral behavior without inventing qualification',()=>{
    const html=renderToStaticMarkup(<RoleScoringProfileEditorView profileId="fixture" searchProfileId="search"
      profile={null} draft={createRoleScoringProfileDraft()} state="ready" error={null} savedNotice={null}
      onDraftChange={vi.fn()} onSave={vi.fn()} onRetryLoad={vi.fn()} onRetrySave={vi.fn()} onReloadLatest={vi.fn()}/>);
    expect(html).toContain('Remote work region weight');
    expect(html).toContain('not the role-family label');
    expect(html).toContain('unknown stays neutral');
    expect(html).toContain('does not verify work authorization or commute distance');
    expect(html).toContain('do not change hard eligibility');
  });
  it('saves and verifies native region weights without changing the input draft',async()=>{
    const original={...createRoleScoringProfileDraft(),role_family:'Operations'};
    const draft=updateRoleScoringWeight(toggleRoleScoringSignal(original,'remote_location',true),'remote_location',40);
    const saved={...draft,id:'profile-v1',version:'v1'};
    const save=vi.fn().mockResolvedValue(saved);
    const readback=vi.fn().mockResolvedValue(saved);
    expect(await saveAndReadbackRoleScoringProfile(save,readback,'fixture','search',draft,null)).toEqual(saved);
    expect(draft.weights.remote_location).toBe(40);
    expect(original.weights.remote_location).toBeUndefined();
    expect(save).toHaveBeenCalledWith('fixture','search',draft,null);
    expect(readback).toHaveBeenCalledWith('fixture','search');
  });
});
