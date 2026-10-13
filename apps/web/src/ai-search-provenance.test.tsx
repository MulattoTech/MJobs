import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import {AICandidatePreview,AISearchProvenance,AIScoreDetails,AIEvaluationFreshness} from './ai-search-provenance';

it('labels stale and unverifiable evaluations without granting new review',()=>{
  expect(renderToStaticMarkup(<AIEvaluationFreshness status="STALE"/>)).toContain('Job or profile changed');
  expect(renderToStaticMarkup(<AIEvaluationFreshness status="UNKNOWN"/>)).toContain('cannot be verified');
  expect(renderToStaticMarkup(<AIEvaluationFreshness/>)).toContain('cannot be verified');
  expect(renderToStaticMarkup(<AIEvaluationFreshness status="CURRENT"/>)).toBe('');
});

it('labels legacy evaluations without inventing search provenance',()=>{
  expect(renderToStaticMarkup(<AISearchProvenance/>)).toContain('Profile-wide evaluation');
});
it('distinguishes freshly grounded profile plans from historical stored scores',()=>{
  const html=renderToStaticMarkup(<AISearchProvenance basis="CURRENT_APPROVED_SKILL_COVERAGE_V1"/>);
  expect(html).toContain('current approved skill evidence');
  expect(html).not.toContain('stored deterministic scores');
});
it('discloses unknown coverage and preserves honest per-skill evidence',()=>{
  const html=renderToStaticMarkup(<AICandidatePreview basis="CURRENT_APPROVED_SKILL_COVERAGE_V1" unscoredCount={2}
    candidates={[{job_id:'j',title:'Engineer',company:'Example',deterministic_score:50,score_evidence:{
      basis:'APPROVED_SKILL_COVERAGE',base_score:50,score:50,limitation:'Evidence coverage is not qualification.',
      scoring_profile:{selection:'APPROVED_SKILL_COVERAGE',profile_version:'v'},scoring_contributions:[{
        dimension:'Python',raw_score:100,weighted_contribution:50,explanation:'Approved positive evidence.'}]}}]}/>);
  expect(html).toContain('2 jobs have unknown skill coverage');
  expect(html).toContain('Evidence coverage is not qualification');
  expect(html).toContain('Python');
  expect(html).not.toContain('Search-weighted score');
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
