import React, { type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ROLE_SCORING_DIMENSIONS,
  RoleScoringProfileEditor,
  RoleScoringProfileEditorView,
  createRoleScoringProfileDraft,
  loadRoleScoringProfile,
  saveAndReadbackRoleScoringProfile,
  toggleRoleScoringSignal,
  updateRoleScoringThreshold,
  updateRoleScoringWeight,
  validateRoleScoringProfileDraft,
  type RoleScoringProfileDraft,
  type RoleScoringProfileEditorViewProps,
  type SavedRoleScoringProfile,
} from './role-scoring-profile-editor';

const profile:SavedRoleScoringProfile = {
  id:'scoring-a',version:'v7',schema_version:1,role_family:'Cloud Operations',
  weights:{skills:70,title:30,ai_intensity:0},thresholds:{skills:65,title:45},
};
const draft:RoleScoringProfileDraft = {
  schema_version:1,role_family:profile.role_family,
  weights:{...profile.weights},thresholds:{...profile.thresholds},
};

function findElement(node:ReactNode,predicate:(element:ReactElement<any>)=>boolean):ReactElement<any>|null {
  if(Array.isArray(node)) {
    for(const child of node) {
      const found=findElement(child,predicate);
      if(found) return found;
    }
    return null;
  }
  if(!React.isValidElement(node)) return null;
  const element=node as ReactElement<any>;
  if(predicate(element)) return element;
  return findElement(element.props.children,predicate);
}

function viewProps(overrides:Partial<RoleScoringProfileEditorViewProps> = {}):RoleScoringProfileEditorViewProps {
  return {
    profileId:'profile-a',searchProfileId:'search-a',profile,draft,state:'ready',error:null,savedNotice:null,
    onDraftChange:vi.fn(),onSave:vi.fn(),onRetryLoad:vi.fn(),onRetrySave:vi.fn(),onReloadLatest:vi.fn(),
    ...overrides,
  };
}

describe('role scoring profile editor',()=>{
  it('keeps occupation names free-text and leaves optional AI intensity disabled by default',()=>{
    expect(ROLE_SCORING_DIMENSIONS).toContain('ai_intensity');
    expect(createRoleScoringProfileDraft().weights.ai_intensity).toBe(0);
    expect(toggleRoleScoringSignal(createRoleScoringProfileDraft(),'ai_intensity',true).weights.ai_intensity).toBeGreaterThan(0);
    expect(createRoleScoringProfileDraft().role_family).toBe('');
  });

  it('preserves other weights and thresholds while editing one signal',()=>{
    const enabled=toggleRoleScoringSignal(draft,'ai_intensity',true);
    const withWeight=updateRoleScoringWeight(enabled,'ai_intensity',35);
    const updated=updateRoleScoringThreshold(withWeight,'ai_intensity',60);
    expect(updated.weights.skills).toBe(70);
    expect(updated.weights.title).toBe(30);
    expect(updated.thresholds.skills).toBe(65);
    expect(updated.weights.ai_intensity).toBe(35);
    expect(updated.thresholds.ai_intensity).toBe(60);
  });

  it('validates role family, enabled signals, finite weights, and soft thresholds',()=>{
    expect(validateRoleScoringProfileDraft(draft)).toEqual([]);
    expect(validateRoleScoringProfileDraft({...draft,role_family:' '})).toContain('Enter a role family between 1 and 100 characters.');
    expect(validateRoleScoringProfileDraft({...draft,weights:{skills:0,title:0}})).toContain('Enable at least one scoring signal before saving.');
    expect(validateRoleScoringProfileDraft({...draft,weights:{skills:101}}).some(error=>error.includes('0 to 100'))).toBe(true);
    expect(validateRoleScoringProfileDraft({...draft,weights:{skills:Number.NaN}}).some(error=>error.includes('finite number'))).toBe(true);
    expect(validateRoleScoringProfileDraft({...draft,thresholds:{skills:-1}}).some(error=>error.includes('0 to 100'))).toBe(true);
    expect(validateRoleScoringProfileDraft({...draft,weights:{skills:40,other:10} as RoleScoringProfileDraft['weights']}).some(error=>error.includes('unsupported'))).toBe(true);
  });

  it('renders selected profile/search attribution and clearly separates advisory scoring from eligibility',()=>{
    const html=renderToStaticMarkup(<RoleScoringProfileEditorView {...viewProps()}/>);
    expect(html).toContain('profile-a');
    expect(html).toContain('search-a');
    expect(html).toContain('scoring-a');
    expect(html).toContain('v7');
    expect(html).toContain('Advisory scoring only');
    expect(html).toContain('do not change hard eligibility');
    expect(html).toContain('Skills soft threshold');
    expect(html).toContain('AI intensity');
    expect(html).toContain('source-provided job metadata');
  });

  it('wires role, signal, weight, and threshold edits without saving automatically',()=>{
    const onDraftChange=vi.fn();
    const onSave=vi.fn();
    const tree=RoleScoringProfileEditorView(viewProps({onDraftChange,onSave}));
    findElement(tree,element=>element.props['aria-label']==='Role family')?.props.onChange({currentTarget:{value:'Systems Engineering'}});
    findElement(tree,element=>element.props['aria-label']==='Enable AI intensity')?.props.onChange({currentTarget:{checked:true}});
    findElement(tree,element=>element.props['aria-label']==='Skills weight')?.props.onChange({currentTarget:{value:'80'}});
    findElement(tree,element=>element.props['aria-label']==='Skills soft threshold')?.props.onChange({currentTarget:{value:'72'}});
    expect(onDraftChange).toHaveBeenNthCalledWith(1,{...draft,role_family:'Systems Engineering'});
    expect(onDraftChange).toHaveBeenNthCalledWith(2,toggleRoleScoringSignal(draft,'ai_intensity',true));
    expect(onDraftChange).toHaveBeenNthCalledWith(3,updateRoleScoringWeight(draft,'skills',80));
    expect(onDraftChange).toHaveBeenNthCalledWith(4,updateRoleScoringThreshold(draft,'skills',72));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('keeps stale profiles locked until latest readback and preserves retryable errors',()=>{
    const stale=renderToStaticMarkup(<RoleScoringProfileEditorView {...viewProps({state:'stale',error:'This scoring profile changed elsewhere.'})}/>);
    expect(stale).toContain('role="alert"');
    expect(stale).toContain('Reload latest profile');
    expect(stale).toContain('disabled=""');
    expect(stale).not.toContain('Retry save');
    const loadError=renderToStaticMarkup(<RoleScoringProfileEditorView {...viewProps({state:'load_error',error:'The scoring profile could not be loaded. Try again.'})}/>);
    expect(loadError).toContain('Retry load');
    const saveError=renderToStaticMarkup(<RoleScoringProfileEditorView {...viewProps({state:'save_error',error:'The save could not be confirmed. Retry or reload latest data.'})}/>);
    expect(saveError).toContain('Retry save');
  });

  it('loads only the selected saved-search scope and sends the current version before readback',async()=>{
    const updated={...profile,version:'v8',weights:{...draft.weights,ai_intensity:35}};
    const load=vi.fn(async(profileId:string,searchProfileId:string)=>{
      expect(profileId).toBe('profile-a');
      expect(searchProfileId).toBe('search-a');
      return profile;
    });
    await expect(loadRoleScoringProfile(load,'profile-a','search-a')).resolves.toEqual(profile);

    const save=vi.fn(async(_profileId:string,_searchProfileId:string,next:RoleScoringProfileDraft,expectedVersion:string|null)=>{
      expect(next.weights.ai_intensity).toBe(35);
      expect(expectedVersion).toBe('v7');
      return updated;
    });
    const readback=vi.fn(async()=>updated);
    await expect(saveAndReadbackRoleScoringProfile(save,readback,'profile-a','search-a',updated,'v7')).resolves.toEqual(updated);
    expect(save).toHaveBeenCalledTimes(1);
    expect(readback).toHaveBeenCalledExactlyOnceWith('profile-a','search-a');
  });

  it('does not report success when persisted readback differs from save response',async()=>{
    const saved={...profile,version:'v8'};
    await expect(saveAndReadbackRoleScoringProfile(
      vi.fn(async()=>saved),vi.fn(async()=>profile),'profile-a','search-a',draft,'v7',
    )).rejects.toMatchObject({kind:'readback_mismatch'});
  });

  it('starts in a safe loading state and requires explicit save',()=>{
    const load=vi.fn(async()=>profile);
    const save=vi.fn(async()=>profile);
    const readback=vi.fn(async()=>profile);
    const html=renderToStaticMarkup(<RoleScoringProfileEditor profileId="profile-a" searchProfileId="search-a" load={load} save={save} readback={readback}/>);
    expect(html).toContain('Loading role-scoring profile');
    expect(html).toContain('Advisory scoring only');
    expect(html).not.toContain('scoring-a');
    expect(save).not.toHaveBeenCalled();
  });
});
