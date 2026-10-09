import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  DynamicApplicationForm,
  getVisibleDynamicFields,
  getDynamicFormBlockers,
  getDynamicFormConfigurationIssues,
  getDynamicFormSessionKey,
  transitionDynamicAnswer,
  transitionDynamicOptionalSection,
  transitionDynamicRepeatableRows,
  type DynamicApplicationField,
} from './dynamic-application-form';

const fields:DynamicApplicationField[]=[
  {id:'employment-type',label:'Employment type',kind:'select',required:true,supported:true,
    options:['Employee','Contractor']},
  {id:'employee-location',label:'Employee work location',kind:'select',required:true,supported:true,
    visible_when:{field_id:'employment-type',equals:'Employee'},options:['Remote','On site']},
  {id:'contract-agency',label:'Contract agency',kind:'text',required:true,supported:true,
    visible_when:{field_id:'employment-type',equals:'Contractor'}},
  {id:'optional-benefits',label:'Optional benefits details',kind:'textarea',required:false,supported:true,
    optional_section_id:'benefits',optional_section_label:'Benefits'},
  {id:'work-history',label:'Work history',kind:'text',required:false,supported:true,
    repeatable:{row_label:'Work history entry',min_rows:0,max_rows:3}},
  {id:'unsupported-required',label:'Unknown employer assessment',kind:'unsupported',required:true,supported:false},
];

describe('dynamic application form manual review',()=>{
  it('updates conditional field visibility from the current reviewed answers',()=>{
    const employee=getVisibleDynamicFields(fields,{'employment-type':'Employee'},new Set(['benefits']));
    expect(employee.map(field=>field.id)).toContain('employee-location');
    expect(employee.map(field=>field.id)).not.toContain('contract-agency');
    const contractor=getVisibleDynamicFields(fields,{'employment-type':'Contractor'},new Set());
    expect(contractor.map(field=>field.id)).toContain('contract-agency');
    expect(contractor.map(field=>field.id)).not.toContain('employee-location');
    expect(contractor.map(field=>field.id)).not.toContain('optional-benefits');
  });

  it('clears dependent answer and review when a parent choice changes',()=>{
    const state=transitionDynamicAnswer(fields,{
      answers:{'employment-type':'Employee','employee-location':'Remote'},
      reviewed:new Set(['employment-type','employee-location']),enabledOptionalSections:new Set(),
    },'employment-type','Contractor');
    expect(state.answers).toEqual({'employment-type':'Contractor'});
    expect(state.reviewed.has('employee-location')).toBe(false);
    expect(state.reviewed.has('employment-type')).toBe(false);
  });

  it('updates dependent dropdown options and clears a stale reviewed choice',()=>{
    const dependent:DynamicApplicationField[]=[
      {id:'department',label:'Department',kind:'select',required:true,supported:true,options:['Operations','Engineering']},
      {id:'team',label:'Team',kind:'select',required:true,supported:true,
        options_when:{field_id:'department',by_value:{Operations:['Facilities','Support'],Engineering:['Platform','Security']}}},
    ];
    expect(getVisibleDynamicFields(dependent,{department:'Operations'},new Set()).map(field=>field.id)).toEqual(['department','team']);
    const state=transitionDynamicAnswer(dependent,{
      answers:{department:'Operations',team:'Support'},reviewed:new Set(['department','team']),enabledOptionalSections:new Set(),
    },'department','Engineering');
    expect(state.answers).toEqual({department:'Engineering'});
    expect(state.reviewed.has('team')).toBe(false);
    expect(getDynamicFormBlockers(dependent,state.answers,new Set(['department']))).toContain('team');
  });

  it('does not let a newly visible required field continue until answered and freshly reviewed',()=>{
    const answers={'employment-type':'Employee'};
    const visible=getVisibleDynamicFields(fields,answers,new Set());
    expect(getDynamicFormBlockers(visible,answers,new Set(['employment-type']))).toContain('employee-location');
    const complete={...answers,'employee-location':'Remote'};
    expect(getDynamicFormBlockers(visible,complete,new Set(['employment-type']))).toContain('employee-location');
    expect(getDynamicFormBlockers(visible,complete,new Set(['employment-type','employee-location']))).not.toContain('employee-location');
  });

  it('renders optional sections and repeatable rows as explicit editable controls with no submission affordance',()=>{
    const html=renderToStaticMarkup(<DynamicApplicationForm provider="mock-ats" profileId="profile-a" applicationId="application-a"
      fields={fields} restoredDraft={{profile_id:'profile-a',application_id:'application-a',answers:{'employment-type':'Employee'}}}
      onContinue={vi.fn()}/>);
    expect(html).toContain('Employment type');
    expect(html).toContain('Employee work location');
    expect(html).toContain('Show optional section: Benefits');
    expect(html).toContain('Add Work history entry');
    expect(html).toContain('Continue');
    expect(html).toContain('does not submit an application');
    expect(html).not.toMatch(/<button[^>]*>[^<]*submit[^<]*<\/button>/i);
  });

  it('fails closed for unsupported required fields and never presents an answer control for them',()=>{
    const visible=fields.filter(field=>field.id==='unsupported-required');
    const blockers=getDynamicFormBlockers(visible,{},new Set());
    expect(blockers).toContain('unsupported-required');
    const html=renderToStaticMarkup(<DynamicApplicationForm provider="mock-ats" profileId="profile-a" applicationId="application-a"
      fields={visible} onContinue={vi.fn()}/>);
    expect(html).toContain('Unknown employer assessment');
    expect(html).toContain('unsupported and needs manual review');
    expect(html).not.toContain('aria-label="Your answer: Unknown employer assessment"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Continue<\/button>/);
  });

  it('restored drafts are unreviewed, while profile/application scope keys differ',()=>{
    const html=renderToStaticMarkup(<DynamicApplicationForm provider="mock-ats" profileId="profile-a" applicationId="application-a"
      fields={[fields[0]]} restoredDraft={{profile_id:'profile-a',application_id:'application-a',answers:{'employment-type':'Employee'}}}
      onContinue={vi.fn()}/>);
    expect(html).toContain('value="Employee"');
    expect(html).toContain('Review this answer for Employment type');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Continue<\/button>/);
    const wrongScope=renderToStaticMarkup(<DynamicApplicationForm provider="mock-ats" profileId="profile-b" applicationId="application-a"
      fields={[fields[0]]} restoredDraft={{profile_id:'profile-a',application_id:'application-a',answers:{'employment-type':'Employee'}}}
      onContinue={vi.fn()}/>);
    expect(wrongScope).toContain('<option value="" selected="">Select an option</option>');
  });

  it('fails closed when the selected provider is not the mock ATS',()=>{
    const html=renderToStaticMarkup(<DynamicApplicationForm provider="greenhouse" profileId="profile-a" applicationId="application-a"
      fields={[fields[0]]} onContinue={vi.fn()}/>);
    expect(html).toContain('supported mock ATS form');
    expect(html).not.toContain('Employment type');
    expect(html).toContain('External submission is OFF');
  });

  it('blocks cyclic or malformed form metadata instead of silently skipping required questions',()=>{
    const cyclic:DynamicApplicationField[]=[
      {id:'first',label:'First',kind:'text',required:true,supported:true,visible_when:{field_id:'second',equals:'yes'}},
      {id:'second',label:'Second',kind:'text',required:true,supported:true,visible_when:{field_id:'first',equals:'yes'}},
    ];
    expect(getDynamicFormConfigurationIssues(cyclic)).toContain('first');
    const html=renderToStaticMarkup(<DynamicApplicationForm provider="mock-ats" profileId="profile-a" applicationId="application-a"
      fields={cyclic} onContinue={vi.fn()}/>);
    expect(html).toContain('Form metadata needs manual review');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Continue<\/button>/);
    expect(getDynamicFormConfigurationIssues([
      {id:'__proto__',label:'Prototype key',kind:'text',required:true,supported:true},
    ])).toContain('__proto__');
  });

  it('allows blank optional fields but requires explicit review for entered optional answers',()=>{
    const optional=fields.filter(field=>field.id==='work-history');
    expect(getDynamicFormBlockers(optional,{},new Set())).toEqual([]);
    expect(getDynamicFormBlockers(optional,{'work-history':['Sample role']},new Set())).toContain('work-history[0]');
    expect(getDynamicFormBlockers(optional,{'work-history':['Sample role']},new Set(['work-history[0]']))).toEqual([]);
    expect(getDynamicFormSessionKey('profile-a','application-a','mock-ats',optional))
      .not.toBe(getDynamicFormSessionKey('profile-b','application-a','mock-ats',optional));
    expect(getDynamicFormSessionKey('profile-a','application-a','mock-ats',optional))
      .not.toBe(getDynamicFormSessionKey('profile-a','application-b','mock-ats',optional));
    expect(getDynamicFormSessionKey('profile-a','application-a','mock-ats',optional))
      .not.toBe(getDynamicFormSessionKey('profile-a','application-a','greenhouse',optional));
  });

  it('shows optional sections only after the user enables them and clears answers when they close',()=>{
    const optional=fields.filter(field=>field.id==='optional-benefits');
    expect(getVisibleDynamicFields(optional,{},new Set()).map(field=>field.id)).toEqual([]);
    const opened=transitionDynamicOptionalSection(optional,{answers:{},reviewed:new Set(),enabledOptionalSections:new Set()},'benefits',true);
    expect(getVisibleDynamicFields(optional,opened.answers,opened.enabledOptionalSections).map(field=>field.id)).toEqual(['optional-benefits']);
    const filled={...opened,answers:{'optional-benefits':'User supplied detail'},reviewed:new Set(['optional-benefits'])};
    const closed=transitionDynamicOptionalSection(optional,filled,'benefits',false);
    expect(closed.answers).toEqual({});
    expect(closed.reviewed.size).toBe(0);
    expect(closed.enabledOptionalSections.has('benefits')).toBe(false);
  });

  it('requires a repeatable row to be reviewed again after editing and preserves reviews when rows shift',()=>{
    const repeatable=fields.filter(field=>field.id==='work-history');
    const state={answers:{'work-history':['Role A','Role B']},reviewed:new Set(['work-history[0]','work-history[1]']),enabledOptionalSections:new Set<string>()};
    const edited=transitionDynamicRepeatableRows(repeatable,state,'work-history',['Role A revised','Role B'],{changedIndex:0});
    expect(edited.reviewed.has('work-history[0]')).toBe(false);
    expect(edited.reviewed.has('work-history[1]')).toBe(true);
    const removed=transitionDynamicRepeatableRows(repeatable,state,'work-history',['Role B'],{removedIndex:0});
    expect(removed.reviewed.has('work-history[0]')).toBe(true);
    expect(removed.reviewed.has('work-history[1]')).toBe(false);
  });
});
