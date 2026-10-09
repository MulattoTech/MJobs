import React, { useState } from 'react';

export type DynamicApplicationFieldKind='text'|'textarea'|'select'|'checkbox'|'unsupported';

export type DynamicApplicationField={
  id:string;
  label:string;
  kind:DynamicApplicationFieldKind;
  required:boolean;
  supported?:boolean;
  sensitive?:boolean;
  reason?:string;
  options?:string[];
  visible_when?:{field_id:string;equals:string|string[]};
  options_when?:{field_id:string;by_value:Record<string,string[]>};
  optional_section_id?:string;
  optional_section_label?:string;
  repeatable?:{row_label:string;min_rows?:number;max_rows?:number};
};

export type DynamicApplicationAnswer=string|string[]|boolean;
export type DynamicApplicationAnswers=Record<string,DynamicApplicationAnswer>;
export type DynamicApplicationFormState={
  answers:DynamicApplicationAnswers;
  reviewed:Set<string>;
  enabledOptionalSections:Set<string>;
};

export type DynamicApplicationContinuePayload={
  profile_id:string;
  application_id:string;
  answers:DynamicApplicationAnswers;
  reviewed_fields:string[];
};

export type DynamicApplicationRestoredDraft={profile_id:string;application_id:string;answers:DynamicApplicationAnswers};

const SUPPORTED_KINDS=new Set<DynamicApplicationFieldKind>(['text','textarea','select','checkbox']);

function answerText(value:DynamicApplicationAnswer|undefined):string {
  if(typeof value==='string')return value.trim();
  if(typeof value==='boolean')return value?'true':'false';
  return '';
}

function fieldSupported(field:DynamicApplicationField):boolean {
  return field.supported===true&&field.sensitive!==true&&SUPPORTED_KINDS.has(field.kind);
}

export function getDynamicFormSessionKey(profileId:string,applicationId:string,provider:string,fields:DynamicApplicationField[]):string {
  try{return JSON.stringify([profileId,applicationId,provider,fields]);}catch{return JSON.stringify([profileId,applicationId,provider,'invalid-form-metadata']);}
}

export function getDynamicFormConfigurationIssues(fields:DynamicApplicationField[]):string[] {
  const issues:string[]=[];
  const counts=new Map<string,number>();
  if(!Array.isArray(fields))return ['form-metadata'];
  if(fields.length>100)return ['form-metadata'];
  for(const field of fields){
    if(!field||typeof field!=='object'||typeof field.id!=='string'){
      issues.push('form-metadata');continue;
    }
    counts.set(field.id,(counts.get(field.id)||0)+1);
  }
  for(const field of fields){
    if(!field||typeof field!=='object'||typeof field.id!=='string')continue;
    if(!/^[A-Za-z0-9_-]{1,80}$/.test(field.id)||['__proto__','constructor','prototype'].includes(field.id)||counts.get(field.id)!==1
      ||typeof field.label!=='string'||!field.label.trim()||field.label.length>300||typeof field.required!=='boolean'
      ||(field.supported!==undefined&&typeof field.supported!=='boolean')
      ||(field.reason!==undefined&&(typeof field.reason!=='string'||field.reason.length>500))
      ||(field.optional_section_label!==undefined&&(typeof field.optional_section_label!=='string'||field.optional_section_label.length>200)))
      issues.push(field.id||'form-metadata');
    for(const source of [field.visible_when?.field_id,field.options_when?.field_id]){
      if(source&&counts.get(source)!==1)issues.push(field.id);
    }
    if(field.optional_section_id&&!/^[A-Za-z0-9_-]{1,80}$/.test(field.optional_section_id))issues.push(field.id);
    if(field.repeatable&&(typeof field.repeatable.row_label!=='string'||!field.repeatable.row_label.trim()
      ||field.repeatable.row_label.length>300
      ||(field.repeatable.min_rows!==undefined&&(!Number.isInteger(field.repeatable.min_rows)||field.repeatable.min_rows<0||field.repeatable.min_rows>20))
      ||(field.repeatable.max_rows!==undefined&&(!Number.isInteger(field.repeatable.max_rows)||field.repeatable.max_rows<1||field.repeatable.max_rows>20))
      ||(field.repeatable.min_rows!==undefined&&field.repeatable.max_rows!==undefined&&field.repeatable.min_rows>field.repeatable.max_rows)))
      issues.push(field.id);
    if(field.visible_when&&(typeof field.visible_when.field_id!=='string'||!field.visible_when.field_id
      ||!(typeof field.visible_when.equals==='string'
      ||(Array.isArray(field.visible_when.equals)&&field.visible_when.equals.length>0
        &&field.visible_when.equals.length<=100&&field.visible_when.equals.every(value=>typeof value==='string'&&value.length<=300)))))
      issues.push(field.id);
    if(field.kind==='select'&&field.options!==undefined&&(!Array.isArray(field.options)||field.options.length>100
      ||field.options.some(option=>typeof option!=='string'||option.length>300)))
      issues.push(field.id);
    if(field.options_when&&(typeof field.options_when.field_id!=='string'||!field.options_when.field_id||!field.options_when.by_value
      ||field.kind!=='select'
      ||typeof field.options_when.by_value!=='object'||Array.isArray(field.options_when.by_value)
      ||Object.keys(field.options_when.by_value).length>100
      ||Object.values(field.options_when.by_value).some(options=>!Array.isArray(options)||options.length>100
        ||options.some(option=>typeof option!=='string'||option.length>300))))
      issues.push(field.id);
  }
  const dependencies=new Map<string,string[]>();
  for(const field of fields){
    if(!field||typeof field!=='object'||typeof field.id!=='string')continue;
    const sources=[field.visible_when?.field_id,field.options_when?.field_id].filter((value):value is string=>typeof value==='string');
    dependencies.set(field.id,sources);
  }
  const visiting=new Set<string>();const visited=new Set<string>();
  const hasCycle=(id:string):boolean=>{
    if(visiting.has(id))return true;
    if(visited.has(id))return false;
    visiting.add(id);
    for(const dependency of dependencies.get(id)||[])if(dependencies.has(dependency)&&hasCycle(dependency))return true;
    visiting.delete(id);visited.add(id);return false;
  };
  for(const id of dependencies.keys())if(hasCycle(id)){issues.push(id);break;}
  return [...new Set(issues)];
}

export function getFieldOptions(field:DynamicApplicationField,answers:DynamicApplicationAnswers):string[] {
  if(field.kind!=='select')return [];
  if(field.options_when){
    const parent=answers[field.options_when.field_id];
    if(typeof parent!=='string')return [];
    const options=field.options_when.by_value?.[parent];
    return Array.isArray(options)?options.filter(option=>typeof option==='string').slice(0,100):[];
  }
  return Array.isArray(field.options)?field.options.filter(option=>typeof option==='string').slice(0,100):[];
}

export function getVisibleDynamicFields(
  fields:DynamicApplicationField[],
  answers:DynamicApplicationAnswers,
  enabledOptionalSections:Set<string>,
):DynamicApplicationField[]{
  const byId=new Map(fields.map(field=>[field.id,field]));
  return fields.filter(field=>{
    if(field.optional_section_id&&!enabledOptionalSections.has(field.optional_section_id))return false;
    const rule=field.visible_when;
    if(!rule)return true;
    const parent=byId.get(rule.field_id);
    if(!parent)return false;
    const parentValue=answers[rule.field_id];
    if(typeof parentValue!=='string')return false;
    const expected=Array.isArray(rule.equals)?rule.equals:[rule.equals];
    return expected.includes(parentValue);
  });
}

function dependentDescendants(fields:DynamicApplicationField[],fieldId:string):Set<string>{
  const result=new Set<string>();
  const pending=[fieldId];
  while(pending.length){
    const source=pending.shift()!;
    for(const field of fields){
      if(!result.has(field.id)&&(field.visible_when?.field_id===source||field.options_when?.field_id===source)){
        result.add(field.id);pending.push(field.id);
      }
    }
  }
  return result;
}

export function transitionDynamicAnswer(
  fields:DynamicApplicationField[],state:DynamicApplicationFormState,fieldId:string,value:DynamicApplicationAnswer,reviewKey=fieldId,
):DynamicApplicationFormState{
  if(JSON.stringify(state.answers[fieldId])===JSON.stringify(value))return state;
  const clears=dependentDescendants(fields,fieldId);
  const answers={...state.answers,[fieldId]:value};
  const reviewed=new Set(state.reviewed);
  reviewed.delete(fieldId);
  reviewed.delete(reviewKey);
  for(const descendant of clears){
    delete answers[descendant];
    reviewed.delete(descendant);
    for(const reviewId of [...reviewed])if(reviewId.startsWith(`${descendant}[`))reviewed.delete(reviewId);
  }
  return {...state,answers,reviewed};
}

export function transitionDynamicRepeatableRows(
  fields:DynamicApplicationField[],state:DynamicApplicationFormState,fieldId:string,rows:string[],
  options:{changedIndex?:number;removedIndex?:number}={},
):DynamicApplicationFormState{
  if(options.removedIndex!==undefined){
    const next=transitionDynamicAnswer(fields,state,fieldId,rows);
    const reviewed=new Set([...next.reviewed].filter(key=>!key.startsWith(`${fieldId}[`)));
    for(const key of state.reviewed){
      const prefix=`${fieldId}[`;
      if(!key.startsWith(prefix)||!key.endsWith(']'))continue;
      const index=Number(key.slice(prefix.length,-1));
      if(!Number.isInteger(index))continue;
      if(index===options.removedIndex)continue;
      reviewed.add(`${fieldId}[${index>options.removedIndex?index-1:index}]`);
    }
    return {...next,reviewed};
  }
  const rowKey=options.changedIndex===undefined?fieldId:`${fieldId}[${options.changedIndex}]`;
  return transitionDynamicAnswer(fields,state,fieldId,rows,rowKey);
}

export function transitionDynamicOptionalSection(
  fields:DynamicApplicationField[],state:DynamicApplicationFormState,sectionId:string,enabled:boolean,
):DynamicApplicationFormState{
  const enabledOptionalSections=new Set(state.enabledOptionalSections);
  if(enabled){enabledOptionalSections.add(sectionId);return {...state,enabledOptionalSections};}
  enabledOptionalSections.delete(sectionId);
  const answers={...state.answers};const reviewed=new Set(state.reviewed);
  const clears=new Set(fields.filter(field=>field.optional_section_id===sectionId).map(field=>field.id));
  for(const id of [...clears])for(const child of dependentDescendants(fields,id))clears.add(child);
  for(const id of clears){delete answers[id];reviewed.delete(id);for(const key of [...reviewed])if(key.startsWith(`${id}[`))reviewed.delete(key);}
  return {...state,answers,reviewed,enabledOptionalSections};
}

export function getDynamicFormBlockers(
  visibleFields:DynamicApplicationField[],answers:DynamicApplicationAnswers,reviewed:Set<string>,
):string[]{
  const blockers:string[]=[];
  for(const field of visibleFields){
    const raw=answers[field.id];
    const supported=fieldSupported(field);
    if(field.repeatable){
      const rows=Array.isArray(raw)?raw:[];
      const minimum=field.repeatable.min_rows??(field.required?1:0);
      if(field.required&&rows.length<minimum){blockers.push(field.id);continue;}
      if(field.required&&!supported){blockers.push(field.id);continue;}
      for(let index=0;index<rows.length;index++){
        if(!answerText(rows[index])){
          if(field.required||rows.length>minimum)blockers.push(`${field.id}[${index}]`);
        }else if(!supported||!reviewed.has(`${field.id}[${index}]`))blockers.push(`${field.id}[${index}]`);
      }
      continue;
    }
    const answered=typeof raw==='boolean'||answerText(raw).length>0;
    if(field.required&&!supported){blockers.push(field.id);continue;}
    if(field.required&&!answered){blockers.push(field.id);continue;}
    if(field.kind==='select'&&answered&&typeof raw==='string'&&!getFieldOptions(field,answers).includes(raw)){
      if(field.required||answered)blockers.push(field.id);
      continue;
    }
    if(answered&&(!supported||!reviewed.has(field.id)))blockers.push(field.id);
  }
  return [...new Set(blockers)];
}

function safeInitialAnswers(fields:DynamicApplicationField[],initial:DynamicApplicationAnswers|undefined):DynamicApplicationAnswers{
  if(!initial||typeof initial!=='object'||Array.isArray(initial))return {};
  const accepted:DynamicApplicationAnswers={};
  for(const field of fields){
    if(!fieldSupported(field)||!Object.prototype.hasOwnProperty.call(initial,field.id))continue;
    const value=initial[field.id];
    if(field.repeatable){
      if(Array.isArray(value)&&value.every(item=>typeof item==='string'&&item.length<=12000))accepted[field.id]=value.slice(0,field.repeatable.max_rows||20);
    }else if(field.kind==='checkbox'&&typeof value==='boolean')accepted[field.id]=value;
    else if(typeof value==='string'&&value.length<=12000)accepted[field.id]=value;
  }
  return accepted;
}

function visibleInitialAnswers(fields:DynamicApplicationField[],answers:DynamicApplicationAnswers):DynamicApplicationAnswers{
  const visible=new Set(getVisibleDynamicFields(fields,answers,new Set()).map(field=>field.id));
  const result={...answers};
  for(const field of fields){
    if(!visible.has(field.id)){delete result[field.id];continue;}
    const value=result[field.id];
    if(field.kind==='select'&&typeof value==='string'&&!getFieldOptions(field,result).includes(value))delete result[field.id];
  }
  return result;
}

function DynamicApplicationFormSession({profileId,applicationId,provider,fields:rawFields,restoredDraft,onContinue}:DynamicApplicationFormProps){
  const fields=Array.isArray(rawFields)?rawFields.slice(0,100).filter((field):field is DynamicApplicationField=>
    !!field&&typeof field==='object'&&typeof field.id==='string'&&typeof field.label==='string'):[];
  const scopedDraft=restoredDraft?.profile_id===profileId&&restoredDraft.application_id===applicationId
    ?restoredDraft.answers:undefined;
  const [answers,setAnswers]=useState<DynamicApplicationAnswers>(()=>visibleInitialAnswers(fields,safeInitialAnswers(fields,scopedDraft)));
  const [reviewed,setReviewed]=useState<Set<string>>(()=>new Set());
  const [enabledOptionalSections,setEnabledOptionalSections]=useState<Set<string>>(()=>new Set());
  const issues=getDynamicFormConfigurationIssues(rawFields);
  const visibleFields=getVisibleDynamicFields(fields,answers,enabledOptionalSections);
  const blockers=getDynamicFormBlockers(visibleFields,answers,reviewed);
  const validScope=!!profileId&&!!applicationId&&provider==='mock-ats';
  const canContinue=validScope&&issues.length===0&&blockers.length===0;
  const optionalSections=[...new Map(fields.filter(field=>typeof field.optional_section_id==='string'&&!!field.optional_section_id)
    .map(field=>[field.optional_section_id!,typeof field.optional_section_label==='string'&&field.optional_section_label
      ?field.optional_section_label:field.optional_section_id![0].toUpperCase()+field.optional_section_id!.slice(1)]))];

  function updateAnswer(fieldId:string,value:DynamicApplicationAnswer){
    const next=transitionDynamicAnswer(fields,{answers,reviewed,enabledOptionalSections},fieldId,value);
    setAnswers(next.answers);setReviewed(next.reviewed);
  }

  function toggleOptionalSection(sectionId:string,enabled:boolean){
    const next=transitionDynamicOptionalSection(fields,{answers,reviewed,enabledOptionalSections},sectionId,enabled);
    setEnabledOptionalSections(next.enabledOptionalSections);setAnswers(next.answers);setReviewed(next.reviewed);
  }

  function renderField(field:DynamicApplicationField){
    const value=answers[field.id];
    const supported=fieldSupported(field);
    const requiredMark=field.required?<span aria-label="required"> (required)</span>:<span> (optional)</span>;
    const controlId=`dynamic-application-${field.id}`;
    if(field.sensitive)return <fieldset className="fact" key={field.id} aria-label={field.label}>
      <legend>{field.label}{requiredMark}</legend><p role={field.required?'alert':'status'}>This field needs separate manual review. This component will not collect or answer sensitive information.</p>
    </fieldset>;
    if(!supported)return <fieldset className="fact" key={field.id} aria-label={field.label}>
      <legend>{field.label}{requiredMark}</legend><p role={field.required?'alert':'status'}>{typeof field.reason==='string'&&field.reason||'This field is unsupported and needs manual review. No answer will be invented or sent.'}</p>
    </fieldset>;
    if(field.repeatable){
      const rows=Array.isArray(value)?value as string[]:[];
      const max=field.repeatable.max_rows??20;
      return <fieldset className="fact" key={field.id} aria-label={field.label}>
        <legend>{field.label}{requiredMark}</legend>
        {rows.map((row,index)=><div className="fact" key={`${field.id}-${index}`}>
          <label htmlFor={`${controlId}-${index}`}>{field.repeatable!.row_label} {index+1}</label>
          <input id={`${controlId}-${index}`} type="text" maxLength={12000} value={row}
            onChange={event=>{const next=[...rows];next[index]=event.target.value;const state=transitionDynamicRepeatableRows(fields,{answers,reviewed,enabledOptionalSections},field.id,next,{changedIndex:index});setAnswers(state.answers);setReviewed(state.reviewed);}}
            aria-label={`${field.repeatable!.row_label} ${index+1}`}/>
          <label className="consent"><input type="checkbox" aria-label={`Review ${field.repeatable!.row_label} ${index+1}`}
            checked={reviewed.has(`${field.id}[${index}]`)} disabled={!row.trim()}
            onChange={event=>setReviewed(current=>{const next=new Set(current);if(event.target.checked)next.add(`${field.id}[${index}]`);else next.delete(`${field.id}[${index}]`);return next;})}/>
            I reviewed this entry.</label>
          <button type="button" onClick={()=>{const next=rows.filter((_,rowIndex)=>rowIndex!==index);const state=transitionDynamicRepeatableRows(fields,{answers,reviewed,enabledOptionalSections},field.id,next,{removedIndex:index});setAnswers(state.answers);setReviewed(state.reviewed);}}>Remove {field.repeatable!.row_label} {index+1}</button>
        </div>)}
        <button type="button" disabled={rows.length>=max} onClick={()=>updateAnswer(field.id,[...rows,''])}>Add {field.repeatable.row_label}</button>
      </fieldset>;
    }
    return <fieldset className="fact" key={field.id} aria-label={field.label}>
      <legend>{field.label}{requiredMark}</legend>
      {field.kind==='select'?<label htmlFor={controlId}>{field.label}<select id={controlId} value={typeof value==='string'?value:''}
          onChange={event=>updateAnswer(field.id,event.target.value)} aria-label={field.label}>
          <option value="">Select an option</option>{getFieldOptions(field,answers).map(option=><option key={option} value={option}>{option}</option>)}
        </select></label>:field.kind==='textarea'?<label htmlFor={controlId}>{field.label}<textarea id={controlId} maxLength={12000}
          value={typeof value==='string'?value:''} onChange={event=>updateAnswer(field.id,event.target.value)} aria-label={field.label}/></label>
        :field.kind==='checkbox'?<label htmlFor={controlId}><input id={controlId} type="checkbox" checked={value===true}
          onChange={event=>updateAnswer(field.id,event.target.checked)} aria-label={field.label}/>{field.label}</label>
        :<label htmlFor={controlId}>{field.label}<input id={controlId} type="text" maxLength={12000}
          value={typeof value==='string'?value:''} onChange={event=>updateAnswer(field.id,event.target.value)} aria-label={field.label}/></label>}
      <label className="consent"><input type="checkbox" aria-label={`Review this answer for ${field.label}`}
        checked={reviewed.has(field.id)} disabled={!((typeof value==='boolean')||(typeof value==='string'&&value.trim()))}
        onChange={event=>setReviewed(current=>{const next=new Set(current);if(event.target.checked)next.add(field.id);else next.delete(field.id);return next;})}/>
        I reviewed this answer.</label>
      <p className="muted">Enter your own response. No career fact or answer is inferred.</p>
    </fieldset>;
  }

  if(!validScope)return <section className="panel" aria-label="Dynamic application form">
    <h2>Application form review</h2><p role="alert">Select an application with the supported mock ATS form before reviewing fields.</p>
    <p className="muted">Continue is unavailable. External submission is OFF.</p>
  </section>;

  return <section className="panel" aria-label="Dynamic application form">
    <h2>Mock ATS application form review</h2>
    <p>Profile {profileId} · Application {applicationId}</p>
    <p className="muted">Review the current visible fields. Answers are entered by you; this form does not infer career facts or submit an application.</p>
    {issues.length>0&&<div role="alert"><strong>Form metadata needs manual review.</strong> Continue is disabled until the dynamic form definition is corrected.</div>}
    {optionalSections.map(([sectionId,label])=><label className="consent" key={sectionId}>
      <input type="checkbox" aria-label={`Show optional section: ${label}`}
        checked={enabledOptionalSections.has(sectionId)} onChange={event=>toggleOptionalSection(sectionId,event.target.checked)}/>
      Show optional section: {label}
    </label>)}
    {visibleFields.map(renderField)}
    {blockers.length>0&&<div role="alert"><strong>Review required before continuing.</strong><ul aria-label="Fields needing review">
      {blockers.map(id=><li key={id}>{visibleFields.find(field=>field.id===id.split('[')[0])?.label||id}</li>)}
    </ul></div>}
    <button type="button" className="primary" disabled={!canContinue} onClick={()=>onContinue({
      profile_id:profileId,application_id:applicationId,answers:{...answers},reviewed_fields:[...reviewed].sort(),
    })}>Continue</button>
    <p className="muted">Continue advances to local application review only. It does not submit an application.</p>
  </section>;
}

export type DynamicApplicationFormProps={
  profileId:string;
  applicationId:string;
  provider:string;
  fields:DynamicApplicationField[];
  restoredDraft?:DynamicApplicationRestoredDraft;
  onContinue:(payload:DynamicApplicationContinuePayload)=>void;
};

export function DynamicApplicationForm(props:DynamicApplicationFormProps){
  const key=getDynamicFormSessionKey(props.profileId,props.applicationId,props.provider,props.fields);
  return <DynamicApplicationFormSession key={key} {...props}/>;
}
