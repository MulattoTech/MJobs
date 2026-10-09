import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationDynamicFieldReview, type DynamicApplicationField } from './application-dynamic-field-review';

describe('application dynamic field review entry point',()=>{
  it('renders the shared mock ATS reviewer with an unreviewed restored answer and no submit action',()=>{
    const fields:DynamicApplicationField[]=[
      {id:'employment-type',label:'Employment type',kind:'select',required:true,supported:true,options:['Employee','Contractor']},
    ];
    const html=renderToStaticMarkup(<ApplicationDynamicFieldReview provider="mock-ats" profileId="profile-a"
      applicationId="application-a" fields={fields}
      restoredDraft={{profile_id:'profile-a',application_id:'application-a',answers:{'employment-type':'Employee'}}}
      onContinue={vi.fn()}/>);
    expect(html).toContain('Mock ATS application form review');
    expect(html).toContain('Review this answer for Employment type');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Continue<\/button>/);
    expect(html).toContain('does not submit an application');
    expect(html).not.toMatch(/<button[^>]*>[^<]*submit[^<]*<\/button>/i);
  });

  it('keeps unsupported required controls in the manual intervention state',()=>{
    const fields:DynamicApplicationField[]=[
      {id:'unknown-essay',label:'Unknown essay response',kind:'unsupported',required:true,supported:false},
    ];
    const html=renderToStaticMarkup(<ApplicationDynamicFieldReview provider="mock-ats" profileId="profile-a"
      applicationId="application-a" fields={fields} onContinue={vi.fn()}/>);
    expect(html).toContain('unsupported and needs manual review');
    expect(html).not.toContain('<textarea');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Continue<\/button>/);
  });
});
