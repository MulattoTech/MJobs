import React, { useEffect, useRef, useState } from 'react';

export const ROLE_SCORING_DIMENSIONS = [
  'skills', 'title', 'salary', 'work_arrangement', 'employment_type',
  'certification', 'education', 'ai_intensity',
] as const;

export type BuiltInRoleScoringDimension = typeof ROLE_SCORING_DIMENSIONS[number];
export type RoleScoringDimension = BuiltInRoleScoringDimension|string;
export type RoleScoringProfileDraft = {
  schema_version:1;
  role_family:string;
  weights:Record<string,number>;
  thresholds:Record<string,number>;
};
export type SavedRoleScoringProfile = RoleScoringProfileDraft & {id:string;version:string};
export type RoleScoringEditorState = 'scope_required'|'loading'|'ready'|'saving'|'load_error'|'save_error'|'stale';

export type LoadRoleScoringProfile = (profileId:string,searchProfileId:string)=>Promise<unknown>;
export type SaveRoleScoringProfile = (
  profileId:string,
  searchProfileId:string,
  draft:RoleScoringProfileDraft,
  expectedVersion:string|null,
)=>Promise<unknown>;
export type ReadbackRoleScoringProfile = LoadRoleScoringProfile;

export class RoleScoringProfileEditorError extends Error {
  constructor(readonly kind:'invalid_scope'|'invalid_profile'|'invalid_draft'|'version_conflict'|'readback_mismatch') {
    super(kind);
    this.name='RoleScoringProfileEditorError';
  }
}

const DIMENSION_LABELS:Record<BuiltInRoleScoringDimension,string> = {
  skills:'Skills',
  title:'Title',
  salary:'Salary',
  work_arrangement:'Work arrangement',
  employment_type:'Employment type',
  certification:'Certification',
  education:'Education',
  ai_intensity:'AI intensity',
};

const DIMENSION_DETAILS:Record<BuiltInRoleScoringDimension,string> = {
  skills:'Approved profile skills compared with evidence in the job posting.',
  title:'The job title compared with this editable role family.',
  salary:'Source-provided compensation compared with saved preferences.',
  work_arrangement:'Source-provided remote, hybrid, or on-site information.',
  employment_type:'Source-provided employment type compared with preferences.',
  certification:'Approved certification facts compared with posting requirements.',
  education:'Approved education facts compared with posting requirements.',
  ai_intensity:'Optional signal from source-provided job metadata; it says nothing about candidate identity.',
};

function isRecord(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}

function validAmount(value:unknown):value is number {
  return typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100;
}

const CUSTOM_DIMENSION_KEY_PATTERN=/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

function isBuiltInDimension(dimension:string):dimension is BuiltInRoleScoringDimension {
  return (ROLE_SCORING_DIMENSIONS as readonly string[]).includes(dimension);
}

export function isStableCustomDimensionKey(value:unknown):value is string {
  return typeof value==='string'&&value.length>=1&&value.length<=64
    &&CUSTOM_DIMENSION_KEY_PATTERN.test(value);
}

export function addCustomScoringDimension(
  draft:RoleScoringProfileDraft,
  dimension:string,
):RoleScoringProfileDraft {
  if(!isStableCustomDimensionKey(dimension)||isBuiltInDimension(dimension)
    ||Object.prototype.hasOwnProperty.call(draft.weights,dimension)
    ||Object.prototype.hasOwnProperty.call(draft.thresholds,dimension)) {
    throw new RoleScoringProfileEditorError('invalid_draft');
  }
  return {...draft,weights:{...draft.weights,[dimension]:0}};
}

export function createRoleScoringProfileDraft(profile?:SavedRoleScoringProfile|null):RoleScoringProfileDraft {
  if(!profile) return {schema_version:1,role_family:'',weights:{ai_intensity:0},thresholds:{}};
  return {
    schema_version:1,
    role_family:profile.role_family,
    weights:{...profile.weights,ai_intensity:profile.weights.ai_intensity??0},
    thresholds:{...profile.thresholds},
  };
}

export function toggleRoleScoringSignal(
  draft:RoleScoringProfileDraft,
  dimension:RoleScoringDimension,
  enabled:boolean,
):RoleScoringProfileDraft {
  const previous=draft.weights[dimension]??0;
  return {
    ...draft,
    weights:{...draft.weights,[dimension]:enabled?(previous>0?previous:25):0},
    thresholds:{...draft.thresholds},
  };
}

export function updateRoleScoringWeight(
  draft:RoleScoringProfileDraft,
  dimension:RoleScoringDimension,
  weight:number,
):RoleScoringProfileDraft {
  return {...draft,weights:{...draft.weights,[dimension]:weight}};
}

export function updateRoleScoringThreshold(
  draft:RoleScoringProfileDraft,
  dimension:RoleScoringDimension,
  threshold:number|null,
):RoleScoringProfileDraft {
  const thresholds={...draft.thresholds};
  if(threshold===null) delete thresholds[dimension];
  else thresholds[dimension]=threshold;
  return {...draft,thresholds};
}

export function validateRoleScoringProfileDraft(value:unknown):string[] {
  if(!isRecord(value)) return ['The scoring profile draft must be an object.'];
  const errors:string[]=[];
  if(value.schema_version!==1) errors.push('Unsupported scoring profile schema version.');
  if(typeof value.role_family!=='string'||!value.role_family.trim()||value.role_family.trim().length>100) {
    errors.push('Enter a role family between 1 and 100 characters.');
  }
  for(const field of ['weights','thresholds'] as const) {
    const values=value[field];
    if(!isRecord(values)) {
      errors.push(`${field} must be an object.`);
      continue;
    }
    for(const [dimension,amount] of Object.entries(values)) {
      if(!isBuiltInDimension(dimension)&&!isStableCustomDimensionKey(dimension)) {
        errors.push(`${field} contains invalid custom dimension key ${dimension}.`);
      } else if(!validAmount(amount)) {
        errors.push(`${field} for ${dimension} must be a finite number from 0 to 100.`);
      }
    }
  }
  if(isRecord(value.weights)&&!Object.values(value.weights).some(amount=>validAmount(amount)&&amount>0)) {
    errors.push('Enable at least one scoring signal before saving.');
  }
  return errors;
}

function normalizeSavedProfile(value:unknown):SavedRoleScoringProfile {
  if(!isRecord(value)||typeof value.id!=='string'||!value.id.trim()
    ||typeof value.version!=='string'||!value.version.trim()
    ||typeof value.role_family!=='string'||!isRecord(value.weights)||!isRecord(value.thresholds)) {
    throw new RoleScoringProfileEditorError('invalid_profile');
  }
  const draft:RoleScoringProfileDraft={
    schema_version:value.schema_version as 1,
    role_family:value.role_family,
    weights:value.weights as RoleScoringProfileDraft['weights'],
    thresholds:value.thresholds as RoleScoringProfileDraft['thresholds'],
  };
  if(validateRoleScoringProfileDraft(draft).some(message=>message!=='Enable at least one scoring signal before saving.')) {
    throw new RoleScoringProfileEditorError('invalid_profile');
  }
  return {id:value.id,version:value.version,schema_version:1,role_family:value.role_family.trim(),
    weights:{...draft.weights,ai_intensity:draft.weights.ai_intensity??0},thresholds:{...draft.thresholds}};
}

function requireScope(profileId:string,searchProfileId:string):void {
  if(typeof profileId!=='string'||!profileId.trim()||typeof searchProfileId!=='string'||!searchProfileId.trim()) {
    throw new RoleScoringProfileEditorError('invalid_scope');
  }
}

export async function loadRoleScoringProfile(
  load:LoadRoleScoringProfile,
  profileId:string,
  searchProfileId:string,
):Promise<SavedRoleScoringProfile|null> {
  requireScope(profileId,searchProfileId);
  const value=await load(profileId,searchProfileId);
  return value===null?null:normalizeSavedProfile(value);
}

function sameConfiguration(left:RoleScoringProfileDraft,right:RoleScoringProfileDraft):boolean {
  const sameMap=(leftMap:Record<string,number>,rightMap:Record<string,number>)=>{
    const leftKeys=Object.keys(leftMap).sort();
    const rightKeys=Object.keys(rightMap).sort();
    return leftKeys.length===rightKeys.length
      &&leftKeys.every((key,index)=>key===rightKeys[index]&&leftMap[key]===rightMap[key]);
  };
  return left.schema_version===right.schema_version&&left.role_family.trim()===right.role_family.trim()
    &&sameMap(left.weights,right.weights)&&sameMap(left.thresholds,right.thresholds);
}

export async function saveAndReadbackRoleScoringProfile(
  save:SaveRoleScoringProfile,
  readback:ReadbackRoleScoringProfile,
  profileId:string,
  searchProfileId:string,
  draft:RoleScoringProfileDraft,
  expectedVersion:string|null,
):Promise<SavedRoleScoringProfile> {
  requireScope(profileId,searchProfileId);
  if(validateRoleScoringProfileDraft(draft).length>0) throw new RoleScoringProfileEditorError('invalid_draft');
  if(expectedVersion!==null&&(typeof expectedVersion!=='string'||!expectedVersion.trim())) {
    throw new RoleScoringProfileEditorError('invalid_draft');
  }
  const saved=normalizeSavedProfile(await save(profileId,searchProfileId,{
    schema_version:1,role_family:draft.role_family.trim(),weights:{...draft.weights},thresholds:{...draft.thresholds},
  },expectedVersion));
  if(expectedVersion!==null&&saved.version===expectedVersion) throw new RoleScoringProfileEditorError('readback_mismatch');
  const confirmedValue=await readback(profileId,searchProfileId);
  if(confirmedValue===null) throw new RoleScoringProfileEditorError('readback_mismatch');
  const confirmed=normalizeSavedProfile(confirmedValue);
  if(saved.id!==confirmed.id||saved.version!==confirmed.version||!sameConfiguration(saved,confirmed)) {
    throw new RoleScoringProfileEditorError('readback_mismatch');
  }
  return confirmed;
}

export type RoleScoringProfileEditorViewProps = {
  profileId:string|null;
  searchProfileId:string|null;
  profile:SavedRoleScoringProfile|null;
  draft:RoleScoringProfileDraft;
  state:RoleScoringEditorState;
  error:string|null;
  savedNotice:string|null;
  onDraftChange:(next:RoleScoringProfileDraft)=>void;
  onSave:()=>void;
  onRetryLoad:()=>void;
  onRetrySave:()=>void;
  onReloadLatest:()=>void;
  customDimensionKey?:string;
  customDimensionError?:string|null;
  onCustomDimensionKeyChange?:(value:string)=>void;
  onAddCustomDimension?:(value:string)=>void;
};

export function RoleScoringProfileEditorView({
  profileId,searchProfileId,profile,draft,state,error,savedNotice,onDraftChange,
  onSave,onRetryLoad,onRetrySave,onReloadLatest,customDimensionKey='',customDimensionError=null,
  onCustomDimensionKeyChange=()=>undefined,onAddCustomDimension=()=>undefined,
}:RoleScoringProfileEditorViewProps) {
  const validation=state==='ready'||state==='save_error'?validateRoleScoringProfileDraft(draft):[];
  const busy=state==='loading'||state==='saving';
  const editable=state==='ready'||state==='save_error';
  const canSave=editable&&validation.length===0;

  return <section className="panel" aria-labelledby="role-scoring-profile-title">
    <h2 id="role-scoring-profile-title">Role-specific scoring profile</h2>
    <p role="note"><strong>Advisory scoring only.</strong> Weights and soft thresholds can change ranking and explanations. They do not change hard eligibility, saved-search filters, or application rules.</p>
    {profileId&&searchProfileId
      ?<p aria-label="Scoring profile scope">Selected profile {profileId}; saved search {searchProfileId}.</p>
      :<p role="status">Select a profile and saved search to edit scoring preferences.</p>}
    <p aria-live="polite">{profile?`Saved scoring profile ${profile.id}, current version ${profile.version}.`:'No scoring profile is saved for this search yet.'}</p>
    {state==='loading'&&<p role="status">Loading role-scoring profile…</p>}
    {error&&<p role="alert">{error}</p>}
    {savedNotice&&<p role="status">{savedNotice}</p>}
    {state==='scope_required'?<p role="status">Choose a profile and saved search before loading this editor.</p>:<>
      <label htmlFor="role-family">Role family</label>
      <input id="role-family" aria-label="Role family" type="text" maxLength={100}
        value={draft.role_family} disabled={!editable}
        onChange={event=>onDraftChange({...draft,role_family:event.currentTarget.value})}/>
      <fieldset disabled={!editable}>
        <legend>Signal weights and soft thresholds</legend>
        <p>Set weights and thresholds from 0 to 100. A zero weight disables a signal; thresholds are soft ranking preferences.</p>
        {ROLE_SCORING_DIMENSIONS.map(dimension=>{
          const label=DIMENSION_LABELS[dimension];
          const enabled=(draft.weights[dimension]??0)>0;
          const optional=dimension==='ai_intensity';
          return <div key={dimension} className="role-scoring-dimension">
            <label>
              <input aria-label={`Enable ${label}`} type="checkbox" checked={enabled}
                onChange={event=>onDraftChange(toggleRoleScoringSignal(draft,dimension,event.currentTarget.checked))}/>
              {label}{optional?' (optional)':''}
            </label>
            <p>{DIMENSION_DETAILS[dimension]}</p>
            <label htmlFor={`weight-${dimension}`}>{label} weight</label>
            <input id={`weight-${dimension}`} aria-label={`${label} weight`} type="number" min={0} max={100} step={1}
              value={draft.weights[dimension]??0} disabled={!enabled}
              onChange={event=>onDraftChange(updateRoleScoringWeight(draft,dimension,Number(event.currentTarget.value)))}/>
            <label htmlFor={`threshold-${dimension}`}>{label} soft threshold</label>
            <input id={`threshold-${dimension}`} aria-label={`${label} soft threshold`} type="number" min={0} max={100} step={1}
              value={draft.thresholds[dimension]??''} disabled={!enabled}
              onChange={event=>{
                const value=event.currentTarget.value;
                onDraftChange(updateRoleScoringThreshold(draft,dimension,value===''?null:Number(value)));
              }}/>
          </div>;
        })}
        {[...new Set([...Object.keys(draft.weights),...Object.keys(draft.thresholds)])]
          .filter(dimension=>!isBuiltInDimension(dimension)).sort().map(dimension=>{
            const enabled=(draft.weights[dimension]??0)>0;
            return <div key={dimension} className="role-scoring-dimension role-scoring-custom-dimension">
              <label>
                <input aria-label={`Enable ${dimension} custom signal`} type="checkbox" checked={enabled}
                  onChange={event=>onDraftChange(toggleRoleScoringSignal(draft,dimension,event.currentTarget.checked))}/>
                {dimension} (custom signal)
              </label>
              <p>Absent explicit source metadata stays UNKNOWN. Custom signals are never inferred from posting prose and never change hard eligibility.</p>
              <label htmlFor={`weight-${dimension}`}>{dimension} weight</label>
              <input id={`weight-${dimension}`} aria-label={`${dimension} weight`} type="number" min={0} max={100} step={1}
                value={draft.weights[dimension]??0} disabled={!enabled}
                onChange={event=>onDraftChange(updateRoleScoringWeight(draft,dimension,Number(event.currentTarget.value)))}/>
              <label htmlFor={`threshold-${dimension}`}>{dimension} soft threshold</label>
              <input id={`threshold-${dimension}`} aria-label={`${dimension} soft threshold`} type="number" min={0} max={100} step={1}
                value={draft.thresholds[dimension]??''} disabled={!enabled}
                onChange={event=>{
                  const value=event.currentTarget.value;
                  onDraftChange(updateRoleScoringThreshold(draft,dimension,value===''?null:Number(value)));
                }}/>
            </div>;
          })}
      </fieldset>
      {validation.length>0&&<ul role="alert" aria-label="Scoring profile validation errors">{validation.map(message=><li key={message}>{message}</li>)}</ul>}
      <label htmlFor="custom-scoring-dimension-key">Custom scoring signal key</label>
      <input id="custom-scoring-dimension-key" aria-label="Custom scoring signal key" type="text" maxLength={64}
        value={customDimensionKey} disabled={!editable}
        onChange={event=>onCustomDimensionKeyChange(event.currentTarget.value)}/>
      <p>Use a unique lowercase snake-case key (1–64 characters). Add a signal only when its scorer supplies explicit source metadata; missing metadata remains UNKNOWN.</p>
      {customDimensionError&&<p role="alert">{customDimensionError}</p>}
      <button type="button" aria-label="Add custom scoring signal" disabled={!editable}
        onClick={()=>onAddCustomDimension(customDimensionKey)}>Add custom scoring signal</button>
      {state==='load_error'&&<button type="button" onClick={onRetryLoad}>Retry load</button>}
      {state==='save_error'&&<button type="button" disabled={!canSave} onClick={onRetrySave}>Retry save</button>}
      <button type="button" disabled={busy||!profileId||!searchProfileId} onClick={onReloadLatest}>
        {state==='stale'?'Reload latest profile':'Reload saved profile'}
      </button>
      <button type="button" disabled={busy||!canSave} onClick={onSave}>{state==='saving'?'Saving profile…':'Save profile'}</button>
    </>}
  </section>;
}

type EditorModel = RoleScoringProfileEditorViewProps & {scopeKey:string};

function modelForScope(scopeKey:string,profileId:string|null,searchProfileId:string|null):EditorModel {
  return {
    scopeKey,profileId,searchProfileId,profile:null,draft:createRoleScoringProfileDraft(),
    state:profileId&&searchProfileId?'loading':'scope_required',error:null,savedNotice:null,
    customDimensionKey:'',customDimensionError:null,
    onDraftChange:()=>undefined,onSave:()=>undefined,onRetryLoad:()=>undefined,onRetrySave:()=>undefined,onReloadLatest:()=>undefined,
    onCustomDimensionKeyChange:()=>undefined,onAddCustomDimension:()=>undefined,
  };
}

function errorIsConflict(value:unknown):boolean {
  return isRecord(value)&&(value.kind==='stale'||value.kind==='version_conflict'||value.status===409);
}

function safeFailureMessage(value:unknown,action:'load'|'save'):string {
  if(errorIsConflict(value)) return 'This scoring profile changed elsewhere. Reload the latest version before saving.';
  return action==='load'
    ?'The scoring profile could not be loaded. Check the connection and retry.'
    :'The scoring profile could not be saved or verified. Retry or reload the latest profile.';
}

function sameScopeModel(model:EditorModel,scopeKey:string):boolean { return model.scopeKey===scopeKey; }

export function RoleScoringProfileEditor({
  profileId,searchProfileId,load,save,readback,
}: {
  profileId:string|null;
  searchProfileId:string|null;
  load:LoadRoleScoringProfile;
  save:SaveRoleScoringProfile;
  readback:ReadbackRoleScoringProfile;
}) {
  const scopeKey=JSON.stringify([profileId,searchProfileId]);
  const [model,setModel]=useState<EditorModel>(()=>modelForScope(scopeKey,profileId,searchProfileId));
  const [reloadCount,setReloadCount]=useState(0);
  const callbacks=useRef({load,save,readback});
  callbacks.current={load,save,readback};

  useEffect(()=>{
    if(!profileId||!searchProfileId) {
      setModel(modelForScope(scopeKey,profileId,searchProfileId));
      return;
    }
    let current=true;
    setModel(modelForScope(scopeKey,profileId,searchProfileId));
    void loadRoleScoringProfile(callbacks.current.load,profileId,searchProfileId).then(profile=>{
      if(current) setModel({...modelForScope(scopeKey,profileId,searchProfileId),profile,draft:createRoleScoringProfileDraft(profile),state:'ready'});
    }).catch(()=>{
      if(current) setModel({...modelForScope(scopeKey,profileId,searchProfileId),state:'load_error',error:'The scoring profile could not be loaded. Check the connection and retry.'});
    });
    return ()=>{ current=false; };
  },[profileId,searchProfileId,reloadCount,scopeKey]);

  const visible=sameScopeModel(model,scopeKey)?model:modelForScope(scopeKey,profileId,searchProfileId);
  const update=(next:Partial<EditorModel>)=>setModel(previous=>sameScopeModel(previous,scopeKey)?{...previous,...next}:previous);

  const loadLatest=async()=>{
    if(!profileId||!searchProfileId) return;
    update({state:'loading',profile:null,draft:createRoleScoringProfileDraft(),error:null,savedNotice:null});
    try {
      const profile=await loadRoleScoringProfile(callbacks.current.readback,profileId,searchProfileId);
      update({profile,draft:createRoleScoringProfileDraft(profile),state:'ready',error:null,savedNotice:null});
    } catch(failure) {
      update({state:errorIsConflict(failure)?'stale':'load_error',error:safeFailureMessage(failure,'load')});
    }
  };

  const saveDraft=async()=>{
    if(!profileId||!searchProfileId||!sameScopeModel(visible,scopeKey)) return;
    if(validateRoleScoringProfileDraft(visible.draft).length>0) return;
    const draftToSave=visible.draft;
    const expectedVersion=visible.profile?.version??null;
    update({state:'saving',error:null,savedNotice:null});
    try {
      const confirmed=await saveAndReadbackRoleScoringProfile(
        callbacks.current.save,callbacks.current.readback,profileId,searchProfileId,draftToSave,expectedVersion,
      );
      update({profile:confirmed,draft:createRoleScoringProfileDraft(confirmed),state:'ready',error:null,
        savedNotice:`Saved and verified ${confirmed.role_family} profile version ${confirmed.version}.`});
    } catch(failure) {
      update({state:errorIsConflict(failure)?'stale':'save_error',error:safeFailureMessage(failure,'save')});
    }
  };

  const retryLoad=()=>{
    update({state:'loading',error:null,savedNotice:null});
    setReloadCount(previous=>previous+1);
  };

  const retrySave=()=>{ void saveDraft(); };
  const onDraftChange=(next:RoleScoringProfileDraft)=>update({draft:next,error:null,savedNotice:null});
  const onCustomDimensionKeyChange=(value:string)=>update({customDimensionKey:value,customDimensionError:null});
  const onAddCustomDimension=(value:string)=>{
    try {
      const next=addCustomScoringDimension(visible.draft,value);
      update({draft:next,customDimensionKey:'',customDimensionError:null,error:null,savedNotice:null});
    } catch {
      update({customDimensionError:'Enter a unique lowercase snake-case key from 1 to 64 characters.'});
    }
  };

  return <RoleScoringProfileEditorView
    {...visible}
    onDraftChange={onDraftChange}
    onSave={()=>{void saveDraft();}}
    onRetryLoad={retryLoad}
    onRetrySave={retrySave}
    onReloadLatest={()=>{void loadLatest();}}
    onCustomDimensionKeyChange={onCustomDimensionKeyChange}
    onAddCustomDimension={onAddCustomDimension}
  />;
}
