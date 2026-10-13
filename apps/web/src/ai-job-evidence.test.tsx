import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import {AIJobSourceEvidence} from './ai-search-provenance';

it('labels missing and legacy evidence as unknown without pretending rationale is grounded',()=>{
  for(const value of [undefined,{status:'UNKNOWN' as const,quotes:[]}]){
    const html=renderToStaticMarkup(<AIJobSourceEvidence value={value}/>);
    expect(html).toContain('No verified posting quotes');
    expect(html).toContain('unverified advisory text');
  }
});
it('shows checked source text with the precise limit and escapes hostile posting markup',()=>{
  const html=renderToStaticMarkup(<AIJobSourceEvidence value={{status:'VERIFIED_QUOTES',quotes:[{field:'description',quote:'<script>alert(1)</script>'}]}}/>);
  expect(html).toContain('Only the quotations are verified');
  expect(html).toContain('Source: captured job description');
  expect(html).not.toContain('<script>');
  expect(html).toContain('&lt;script&gt;');
});
