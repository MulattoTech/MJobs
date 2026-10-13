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
    {id:'contact',title:'Contact and answer',field_keys:['full_name','email','weekend_availability']},
    {id:'documents',title:'Documents',field_keys:['resume','cover_letter','supporting_documents']},
  ],
} as any;
const selection={available:[],selected_revision_ids:[],unavailable_revision_ids:[]};
const reviewLabel='I reviewed these fixture values, selected documents, and the ordered page plan';
const invalidPlanMessage='Page plan is missing or invalid. Review and run are blocked until the fixture provides the Contact and answer page followed by Documents with all expected fields.';

function renderLoadedRun(run:unknown,previewState:unknown=preview,checked=false) {
  hookState.values=[previewState,run,null,false,selection,[],checked,false,false,''];
  return renderToStaticMarkup(<ControlledApplicationDryRun profileId="profile-a" applicationId="application-a"/>);
}

function visibleText(html:string) {
  return html.replace(/<!--[\s\S]*?-->/g,' ')
    .replace(/<[^>]*>/g,' ')
    .replace(/&(?:amp|lt|gt|quot|#39);/g,entity=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"})[entity]||entity)
    .replace(/\s+/g,' ').trim();
}

function labelInputIsDisabled(html:string,label:string) {
  return [...html.matchAll(/<label\b[^>]*>([\s\S]*?)<\/label>/g)].some(([markup,contents])=>
    visibleText(contents).includes(label)&&/<input\b[^>]*\bdisabled(?:=|\s|>)/.test(contents));
}

function buttonIsDisabled(html:string,label:string) {
  return [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].some(([markup])=>
    visibleText(markup.replace(/^<button\b[^>]*>/,'').replace(/<\/button>$/,''))===label&&/<button\b[^>]*\bdisabled(?:=|\s|>)/.test(markup));
}

describe('controlled application page proof',()=>{
  afterEach(()=>{hookState.values=[];});

  it('shows the supplied ordered page plan before the review acknowledgement',()=>{
    const html=renderLoadedRun(null);
    const text=visibleText(html);
    const plan=html.indexOf('aria-label="Ordered fixture page plan"');
    const contact=text.indexOf('Contact and answer');
    const documents=text.indexOf('Documents');
    const review=text.indexOf('I reviewed these fixture values');

    expect(plan).toBeGreaterThanOrEqual(0);
    expect(documents).toBeGreaterThan(contact);
    expect(text.slice(review)).toContain('ordered page plan');
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

  it('blocks review and running when the ordered page plan is missing',()=>{
    const malformed={...preview,pages:undefined,latest_review:{fingerprint:'fixture-fingerprint'}};
    const html=renderLoadedRun(null,malformed,true);
    const text=visibleText(html);

    expect(text).toContain(invalidPlanMessage);
    expect(labelInputIsDisabled(html,reviewLabel)).toBe(true);
    expect(buttonIsDisabled(html,'Record fixture review')).toBe(true);
    expect(buttonIsDisabled(html,'Run controlled fixture')).toBe(true);
  });

  it('blocks review and running when pages are reversed',()=>{
    const malformed={...preview,pages:[...preview.pages].reverse(),latest_review:{fingerprint:'fixture-fingerprint'}};
    const html=renderLoadedRun(null,malformed,true);
    const text=visibleText(html);

    expect(text).toContain(invalidPlanMessage);
    expect(labelInputIsDisabled(html,reviewLabel)).toBe(true);
    expect(buttonIsDisabled(html,'Record fixture review')).toBe(true);
    expect(buttonIsDisabled(html,'Run controlled fixture')).toBe(true);
  });
});
