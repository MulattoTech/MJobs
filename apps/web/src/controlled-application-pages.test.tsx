import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {afterEach,describe,expect,it,vi} from 'vitest';

const hookState=vi.hoisted(()=>({values:[] as unknown[]}));
vi.mock('react',async importOriginal=>{
  const actual=await importOriginal<typeof import('react')>();
  return {...actual,useState:(_initial:unknown)=>[hookState.values.shift(),vi.fn()],useEffect:()=>{}};
});

import {ControlledApplicationDryRun} from './controlled-application-dry-run';

const preview={
  provider:'greenhouse',context:'CONTROLLED_FIXTURE',fingerprint:'fixture-fingerprint',ready:true,
  fields:[],blockers:[],
  pages:[
    {id:'contact',title:'Contact information',field_keys:['email']},
    {id:'documents',title:'Documents',field_keys:['resume']},
  ],
} as any;
const selection={available:[],selected_revision_ids:[],unavailable_revision_ids:[]};

function renderLoadedRun(run:unknown) {
  hookState.values=[preview,run,null,false,selection,[],false,false,false,''];
  return renderToStaticMarkup(<ControlledApplicationDryRun profileId="profile-a" applicationId="application-a"/>);
}

describe('controlled application page proof',()=>{
  afterEach(()=>{hookState.values=[];});

  it('shows the supplied ordered page plan before the review acknowledgement',()=>{
    const html=renderLoadedRun(null);
    const plan=html.indexOf('aria-label="Ordered fixture page plan"');
    const contact=html.indexOf('Contact information');
    const documents=html.indexOf('Documents');
    const review=html.indexOf('I reviewed these fixture values, selected documents, and the ordered page plan');

    expect(plan).toBeGreaterThanOrEqual(0);
    expect(contact).toBeGreaterThan(plan);
    expect(documents).toBeGreaterThan(contact);
    expect(review).toBeGreaterThan(documents);
  });

  it('shows page proof from the loaded latest receipt',()=>{
    const html=renderLoadedRun({request_id:'run-current',state:'COMPLETED',error_code:null,fingerprint:'fixture-fingerprint',receipt:{
      fingerprint:'fixture-fingerprint',documents:[{kind:'RESUME',document_id:'document-1',size_bytes:42,sha256:'a'.repeat(64)}],
      verification:'VERIFIED',provider_acceptance_claimed:false,
      pages:[{id:'contact',control_count:2,validation:'PASS'},{id:'documents',control_count:1,validation:'PASS'}],
    }});

    expect(html).toContain('aria-label="Saved fixture page proof"');
    expect(html).toContain('Saved page proof');
    expect(html).toContain('Contact');
    expect(html).toContain('Documents');
    expect(html).toContain('2 controls');
    expect(html).toContain('PASS');
  });

  it('keeps legacy receipt documents readable without inventing page proof',()=>{
    const html=renderLoadedRun({request_id:'run-legacy',state:'COMPLETED',error_code:null,fingerprint:'fixture-fingerprint',receipt:{
      fingerprint:'fixture-fingerprint',documents:[{kind:'RESUME',document_id:'document-legacy',size_bytes:42,sha256:'b'.repeat(64)}],
      verification:'VERIFIED',provider_acceptance_claimed:false,
    }});

    expect(html).toContain('RESUME · 42 bytes');
    expect(html).toContain('b'.repeat(64));
    expect(html).not.toContain('Saved fixture page proof');
    expect(html).not.toContain('Saved page proof');
  });
});
