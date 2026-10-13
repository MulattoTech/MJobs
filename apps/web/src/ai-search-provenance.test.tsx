import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import {AICandidatePreview,AISearchProvenance,AIScoreDetails} from './ai-search-provenance';

it('labels legacy evaluations without inventing search provenance',()=>{
  expect(renderToStaticMarkup(<AISearchProvenance/>)).toContain('Profile-wide evaluation');
});
it('shows captured search and escapes untrusted labels',()=>{
  const html=renderToStaticMarkup(<AICandidatePreview scope={{search_profile_id:'s',name:'<script>bad</script>',criteria_digest:'a'.repeat(64),updated_at:'now'}}
    candidates={[{job_id:'j',title:'Engineer',company:'Example',deterministic_score:95,score_evidence:null}]}/>);
  expect(html).toContain('later search edits do not change this result');
  expect(html).toContain('95');expect(html).toContain('&lt;script&gt;');expect(html).not.toContain('<script>');
});
it('keeps missing evidence UNKNOWN rather than showing zero',()=>{
  const html=renderToStaticMarkup(<AIScoreDetails value={{base_score:50,score:50,
    scoring_profile:{selection:'ROLE_FAMILY',profile_version:'v'},scoring_contributions:[{
      dimension:'ai_intensity',raw_score:null,weighted_contribution:null,explanation:'No supported signal.'}]}}/>);
  expect(html).toContain('ai_intensity: UNKNOWN');expect(html).toContain('contribution UNKNOWN');
});
