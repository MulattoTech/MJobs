import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import {AICandidatePreview,AISearchProvenance,AIScoreDetails,AIEvaluationFreshness,AIProviderAccess,
  type AIJobScoringDataFlow,type AIProviderAccessInfo} from './ai-search-provenance';

const recordedDataFlow=():Extract<AIJobScoringDataFlow,{status:'RECORDED'}>=>({
  status:'RECORDED',contract:'JOB_SCORING_V1',
  prompt_fields:['job_title','company','job_description','deterministic_match_score'],
  text_limits:{job_title:512,company:512,job_description:8000,prompt:12000},
  local_records:['evaluation_input_snapshot','advisory_results'],provider_retention:'UNKNOWN',
});
const recordedAccess=():AIProviderAccessInfo=>({provider:'openai',credential_source:'SAVED_PROFILE',
  destination:'https://api.openai.com',data_flow:recordedDataFlow()});

it('discloses the saved profile credential and fixed destination without key material',()=>{
  const html=renderToStaticMarkup(<AIProviderAccess value={{provider:'openai',credential_source:'SAVED_PROFILE',destination:'https://api.openai.com'}}/>);
  expect(html).toContain("this profile&#x27;s saved OpenAI credential");
  expect(html).toContain('https://api.openai.com');
  expect(html).toContain('key change requires a new plan');
});
it('distinguishes runtime, disabled and legacy credential provenance',()=>{
  expect(renderToStaticMarkup(<AIProviderAccess value={{provider:'ollama',credential_source:'RUNTIME_CONFIGURATION',destination:null}}/>)).toContain('runtime configuration');
  expect(renderToStaticMarkup(<AIProviderAccess value={{provider:'disabled',credential_source:'DISABLED',destination:null}}/>)).toContain('no model calls');
  expect(renderToStaticMarkup(<AIProviderAccess/>)).toContain('not recorded');
});

it('shows the recorded job-scoring disclosure in the real candidate preview',()=>{
  const html=renderToStaticMarkup(<AICandidatePreview providerAccess={recordedAccess()} candidates={[]}/>);
  expect(html).toContain('<details aria-label="Job scoring data flow">');
  expect(html).toContain('What job scoring shares and keeps');
  expect(html).toContain('https://api.openai.com');
  expect(html).toContain('Preview makes no model calls');
  expect(html).toContain('Evaluation requires an explicit request');
  expect(html).toContain('does not prove a request was sent');
  expect(html).toContain('job title and company (up to 512 characters each)');
  expect(html).toContain('job description (up to 8,000 characters)');
  expect(html).toContain('deterministic match score');
  expect(html).toContain('derived from profile information');
  expect(html).toContain('12,000 characters');
  expect(html).toContain('does not add raw résumé text, contact details, career facts, or application answers');
  expect(html).toContain('Job source text may itself contain sensitive information');
  expect(html).toContain('MJobs stores the evaluation input snapshot and advisory results in its database');
  expect(html).toContain('Provider retention and deletion are unknown');
  expect(html).toContain('cleaning up MJobs data does not establish deletion at the provider');
  expect(html).toContain('applies only to job scoring');
  expect(html).toContain('does not submit an application');
  expect(html).toContain('document preparation requires a separate review');
  expect(html).not.toContain('on this device');
  expect(html).not.toContain('JOB_SCORING_V1');
});

it('keeps runtime destination unknown even when the provider name sounds local',()=>{
  const html=renderToStaticMarkup(<AIProviderAccess value={{provider:'ollama',
    credential_source:'RUNTIME_CONFIGURATION',destination:null,data_flow:recordedDataFlow()}}/>);
  expect(html).toContain('runtime configuration (ollama)');
  expect(html).toContain('The destination was not recorded for this plan');
  expect(html).toContain('What job scoring shares and keeps');
  expect(html).not.toContain('loopback');
  expect(html).not.toContain('stays local');
  expect(html).not.toContain('on this device');
});

it.each(['openai','ollama','openai-compatible','local','azure-openai','azure'])(
  'accepts the recorded contract for the known runtime provider %s',provider=>{
    const html=renderToStaticMarkup(<AIProviderAccess value={{provider,credential_source:'RUNTIME_CONFIGURATION',
      destination:null,data_flow:recordedDataFlow()}}/>);
    expect(html).toContain('What job scoring shares and keeps');
    expect(html).toContain('The destination was not recorded for this plan');
  });

it.each([
  ['missing',undefined],['null',null],['unknown','future-private-provider'],['empty',''],
  ['number',42],['boolean',true],['object',{name:'private-sentinel'}],['array',['private-sentinel']],
])('rejects %s runtime provider metadata without making recorded disclosure claims',(_label,provider)=>{
  const value={provider,credential_source:'RUNTIME_CONFIGURATION',destination:null,data_flow:recordedDataFlow()};
  const html=renderToStaticMarkup(<AIProviderAccess value={value as AIProviderAccessInfo}/>);
  expect(html).toContain('unknown provider');
  expect(html).toContain('Data-flow details were not recorded or cannot be verified');
  expect(html).not.toContain('What job scoring shares and keeps');
  expect(html).not.toContain('MJobs stores');
  expect(html).not.toContain('future-private-provider');
  expect(html).not.toContain('private-sentinel');
});

it('keeps a newly recorded disabled plan separate from active inference disclosure',()=>{
  const html=renderToStaticMarkup(<AIProviderAccess value={{provider:'disabled',credential_source:'DISABLED',
    destination:null,data_flow:{status:'DISABLED',contract:'JOB_SCORING_V1'}}}/>);
  expect(html).toContain('Job-scoring AI is disabled for this plan');
  expect(html).toContain('authorizes no inference request');
  expect(html).not.toContain('What job scoring shares and keeps');
  expect(html).not.toContain('MJobs stores');
});

it.each([
  ['absent',undefined],
  ['null',null],
  ['unrecorded',{status:'UNKNOWN'}],
  ['future version',{...recordedDataFlow(),contract:'JOB_SCORING_V2'}],
  ['missing limit',{...recordedDataFlow(),text_limits:{job_title:512,company:512,job_description:8000}}],
  ['changed limit',{...recordedDataFlow(),text_limits:{...recordedDataFlow().text_limits,job_description:9000}}],
  ['changed fields',{...recordedDataFlow(),prompt_fields:['resume_text']}],
  ['extra field',{...recordedDataFlow(),private_note:'private-sentinel'}],
  ['extra limit',{...recordedDataFlow(),text_limits:{...recordedDataFlow().text_limits,private_note:'private-sentinel'}}],
  ['different retention',{...recordedDataFlow(),provider_retention:'DELETED'}],
  ['different storage',{...recordedDataFlow(),local_records:['remote_provider']}],
  ['array',[recordedDataFlow()]],
  ['text','private-sentinel'],
])('does not invent recorded data-flow claims for %s metadata',(_label,dataFlow)=>{
  const html=renderToStaticMarkup(<AIProviderAccess value={{...recordedAccess(),data_flow:dataFlow as AIJobScoringDataFlow}}/>);
  expect(html).toContain('Data-flow details were not recorded or cannot be verified');
  expect(html).not.toContain('What job scoring shares and keeps');
  expect(html).not.toContain('derived from profile information');
  expect(html).not.toContain('MJobs stores');
  expect(html).not.toContain('private-sentinel');
});

it('does not backfill a legacy saved result with the current disclosure',()=>{
  const html=renderToStaticMarkup(<AIProviderAccess/>);
  expect(html).toContain('Credential source was not recorded');
  expect(html).toContain('Data-flow details were not recorded');
  expect(html).not.toContain('What job scoring shares and keeps');
  expect(html).not.toContain('MJobs stores');
});

it.each([
  {provider:'disabled',credential_source:'DISABLED',destination:null,data_flow:recordedDataFlow()},
  {provider:'openai',credential_source:'SAVED_PROFILE',destination:'https://other.example',data_flow:recordedDataFlow()},
  {provider:'ollama',credential_source:'RUNTIME_CONFIGURATION',destination:null,data_flow:{status:'DISABLED',contract:'JOB_SCORING_V1'}},
])('does not accept a disclosure inconsistent with the provider binding',value=>{
  const html=renderToStaticMarkup(<AIProviderAccess value={value as AIProviderAccessInfo}/>);
  expect(html).toContain('Data-flow details were not recorded or cannot be verified');
  expect(html).not.toContain('What job scoring shares and keeps');
  expect(html).not.toContain('Job-scoring AI is disabled for this plan');
});

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
