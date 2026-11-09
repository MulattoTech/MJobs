import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { coverLetterLineDiff, DocumentHistory, type GeneratedDocument, type SavedResumeQualityAudit } from './document-history';

const historical:GeneratedDocument={id:'old',profile_id:'fixture',kind:'resume',version:1,profile_version:'snapshot-1',fact_ids:['python'],
  files:{pdf:'resume.pdf'},sha256:{pdf:'abc'},template:'verified-facts-v1',created_at:null,generation_model:null,
  facts_used:[],missing_fact_ids:['python']};
describe('document provenance',()=>{
  it('reports deterministic removed and added cover-letter lines in order',()=>{
    expect(coverLetterLineDiff('Opening\nRemoved detail\nClosing','Opening\nAdded detail\nClosing')).toEqual([
      {kind:'Removed',line:'Removed detail'},
      {kind:'Added',line:'Added detail'},
    ]);
    expect(coverLetterLineDiff('Same line','Same line')).toEqual([]);
  });
  it('renders a contact-only base contract without skill-order fields',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,template:'approved-contact-base-v1',generation_model:'deterministic-contact-copy-v1',generation_context:{mode:'APPROVED_CONTACT_HEADER',contact_header:['Alex','Phone: 555 0100']},missing_fact_ids:[],facts_used:[{fact_id:'python',kind:'skill',text:'Python',source:'Review',confidence:100,verified:true,user_approved:true}]}]}/>);
    expect(html).toContain('Saved contact details included');expect(html).toContain('Phone: 555 0100');expect(html).not.toContain('Tailored resume:');
  });
  it('describes explicit omissions without claiming all work history was retained',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,template:'approved-fact-selection-headings-v1',generation_model:'deterministic-fact-selection-v1',generation_context:{mode:'APPROVED_SKILL_ORDER',job_title:'Example role',job_skills:[],matched_skills:{},original_fact_ids:['python'],ordered_fact_ids:['python'],selection_mode:'EXPLICIT_APPROVED_FACTS',selected_fact_ids:['python'],omitted_fact_ids:['sql']}}]}/>);
    expect(html).toContain('Tailored resume: selected facts');expect(html).toContain('Approved facts included: 1. Omitted from this resume: 1.');expect(html).not.toContain('work history preserved');
  });
  it('shows the exact job responsibility phrase that selected an approved fact',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,template:'tailored-responsibility-v1',generation_model:'deterministic-responsibility-tailoring-v1',missing_fact_ids:[],
      generation_context:{mode:'APPROVED_SKILL_ORDER',job_title:'Systems Administrator',job_skills:[],matched_skills:{},original_fact_ids:['duty'],ordered_fact_ids:['duty'],
        bullet_adjustment:{mode:'EXACT_TARGET_RESPONSIBILITY_PHRASE_BULLET_FACTS',matched_responsibilities:{duty:['maintain nightly database backups for four clinics']},selected_reasons:{duty:['EXACT_TARGET_RESPONSIBILITY_PHRASE']} }},
      facts_used:[{fact_id:'duty',kind:'responsibility',text:'Maintain nightly database backups for four clinics.',source:'Reviewed profile fact',confidence:100,verified:true,user_approved:true}]}]}/>);
    expect(html).toContain('Selected for this job responsibility: maintain nightly database backups for four clinics');
  });
  it('keeps the selected role-lens name, version, reason, and evidence count in document history',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,generation_context:{mode:'APPROVED_SKILL_ORDER',job_title:'Systems Administrator',
      role_lens:{id:'systems',version:1,display_name:'Systems',selection_reason:'EXPLICIT',fact_ids:['linux','iam'],emphasized_fact_ids:['linux'],story_ids:['story-1'],emphasized_story_ids:['story-1']}}}]}/>);
    expect(html).toContain('Role lens: Systems · v1 · explicitly selected for this job');
    expect(html).toContain('2 approved facts and 1 approved story');
  });
  it('shows missing historical evidence without invented timestamps or dead format links',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[historical]}/>);
    expect(html).toContain('Not recorded for this historical document');
    expect(html).toContain('Some original facts are unavailable: python');
    expect(html).toContain('Download PDF');
    expect(html).toContain('href="/profiles/fixture/documents/old/pdf"');
    expect(html).not.toContain('href="/documents/old/pdf"');
    expect(html).not.toContain('Download TXT');
    expect(html).not.toContain('Download DOCX');
  });
  it('shows original fact sources, method, hashes and available outputs with escaped text',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,created_at:'2026-10-04T12:00:00Z',generation_model:'deterministic-fact-copy-v1',files:{txt:'resume.txt'},missing_fact_ids:[],
      facts_used:[{fact_id:'python',kind:'skill',text:'<script>example</script>',source:'Original reviewed resume',confidence:100,verified:true,user_approved:true}]}]}/>);
    expect(html).toContain('Original reviewed resume');expect(html).toContain('deterministic-fact-copy-v1');
    expect(html).toContain('Download TXT');expect(html).toContain('SHA-256');
    expect(html).not.toContain('<script>');expect(html).not.toContain('Not recorded');
  });
  it('uses a profile-scoped download route for reviewed answer exports',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,id:'answer-export',profile_id:'profile-1',
      kind:'application-answer-export',files:{txt:'answers.txt'},sha256:{txt:'abc'},template:'reviewed-application-answers-v1',missing_fact_ids:[],
      generation_context:{mode:'REVIEWED_APPLICATION_ANSWERS',items:[{question:'Work authorization?',source:'Manual',confidence:null,answer_mode:'USER_ENTERED'}]}}]}/>);
    expect(html).toContain('/profiles/profile-1/documents/answer-export/txt');
    expect(html).not.toContain('href="/documents/answer-export/txt"');
  });
  it('shows an integrity-validated job-specific cover-letter preview and blocks downloads after failed readback',()=>{
    const letter:GeneratedDocument={...historical,kind:'cover-letter',files:{pdf:'letter.pdf',docx:'letter.docx',txt:'letter.txt'},missing_fact_ids:[],
      generation_model:'deterministic-job-evidence-v1',template:'job-specific-approved-facts-v1',
      generation_context:{mode:'JOB_SPECIFIC_APPROVED_FACTS',style:'warm',length:'detailed',skills:['Python'],skill_strengths:{Python:'UNKNOWN'},responsibilities:['Build Python services.'],company_identity:'USER_ENTERED_UNVERIFIED'},
      preview_text:'Application for Platform Engineer at Example.\nBuild Python services.\nApproved facts: Python.',preview_error:null};
    const html=renderToStaticMarkup(<DocumentHistory documents={[letter]}/>);
    expect(html).toContain('Job-specific deterministic draft');expect(html).toContain('warm');
    expect(html).toContain('Employer identity: entered manually; verify against the source');
    expect(html).toContain('Preview validated cover letter');expect(html).toContain('Build Python services.');
    expect(html).toContain('Download PDF');expect(html).toContain('Download TXT');

    const blocked=renderToStaticMarkup(<DocumentHistory documents={[{...letter,preview_text:null,preview_error:'Cover-letter validation failed.'}]}/>);
    expect(blocked).toContain('Downloads are disabled until a valid version is prepared');
    expect(blocked).not.toContain('Download PDF');expect(blocked).not.toContain('Download TXT');
  });
  it('shows exact reused story revision and linked fact provenance in document history',()=>{
    const letter:GeneratedDocument={...historical,kind:'cover-letter',files:{txt:'letter.txt'},missing_fact_ids:[],
      generation_context:{mode:'JOB_SPECIFIC_APPROVED_FACTS',style:'professional',length:'normal',skills:[],skill_strengths:{},responsibilities:[],company_identity:'SOURCE_LISTED',review_required:true,
        story_refs:[{story_id:'story-revision-7',revision:2,title:'Reduced report latency',profile_version:'snapshot-1',fact_ids:['python'],fact_fingerprints:[{fact_id:'python',fingerprint:'fact-source-v1'}],text_sha256:'story-content-hash'}]}};
    const html=renderToStaticMarkup(<DocumentHistory documents={[letter]}/>);
    expect(html).toContain('Reused reviewed story revisions');expect(html).toContain('user-authored text preserved verbatim');
    expect(html).toContain('Reduced report latency · revision 2 · linked facts python');
  });
  it('previews a validated resume and shows the selected story provenance',()=>{
    const resume:GeneratedDocument={...historical,kind:'resume',files:{pdf:'resume.pdf',txt:'resume.txt'},missing_fact_ids:[],
      preview_text:'Systems Administrator\nReviewed career stories\nReduced report latency\nI improved the reviewed Python service.',preview_error:null,
      generation_context:{mode:'APPROVED_FACTS',review_required:true,
        story_refs:[{story_id:'story-revision-7',revision:2,title:'Reduced report latency',profile_version:'snapshot-1',fact_ids:['python'],fact_fingerprints:[{fact_id:'python',fingerprint:'fact-source-v1'}],text_sha256:'story-content-hash'}]}};
    const html=renderToStaticMarkup(<DocumentHistory documents={[resume]}/>);
    expect(html).toContain('Preview validated resume');
    expect(html).toContain('I improved the reviewed Python service.');
    expect(html).toContain('exact user-authored wording');
    expect(html).toContain('Reduced report latency · revision 2 · linked facts python');
    expect(html).toContain('Download PDF');
    const blocked=renderToStaticMarkup(<DocumentHistory documents={[{...resume,preview_text:null,preview_error:'Resume validation failed.'}]}/>);
    expect(blocked).toContain('Downloads are disabled until a valid version is prepared');
    expect(blocked).not.toContain('Download PDF');
  });
  it('labels a legacy cover letter whose original job snapshot was not recorded',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,kind:'cover-letter',legacy_job_context_missing:true,
      preview_text:'Application for Earlier Title',preview_error:null,files:{txt:'letter.txt'},missing_fact_ids:[]}]}/>);
    expect(html).toContain('Legacy letter: its original title is preserved from the saved document; the current job title may differ.');
    expect(html).toContain('Application for Earlier Title');
  });
  it('offers only same-application immutable cover-letter revisions for comparison',()=>{
    const first={...historical,id:'letter-1',application_id:'application-a',profile_id:'fixture',kind:'cover-letter',version:1,preview_text:'Earlier letter',files:{txt:'letter-1.txt'},missing_fact_ids:[]};
    const second={...first,id:'letter-2',version:2,preview_text:'Later letter'};
    const other={...first,id:'other-app-letter',application_id:'application-b',version:3,preview_text:'Different application'};
    const html=renderToStaticMarkup(<DocumentHistory documents={[first,second]}/>);
    expect(html).toContain('Compare cover-letter revisions');expect(html).toContain('Earlier cover-letter version');
    expect(html).toContain('Version 1');expect(html).toContain('Version 2');
    const mixed=renderToStaticMarkup(<DocumentHistory documents={[first,second,other]}/>);
    expect(mixed).toContain('Open one application’s document history');expect(mixed).not.toContain('Earlier cover-letter version');
  });
});

describe('AI-assisted resume attribution',()=>{
  it('separates reviewed AI wording from the deterministic renderer and original fact source',()=>{
    const resume:GeneratedDocument={...historical,profile_version:'original-snapshot',preview_error:null,missing_fact_ids:[],
      generation_model:'deterministic-reviewed-renderer',
      generation_context:{mode:'APPROVED_SKILL_ORDER',ai_proposal:{provider:'synthetic-provider',model:'fixture-model',draft_id:'draft',source_profile_version:'original-snapshot',source_fact_ids:['python'],review_policy:'USER_CONFIRMED_FACTUAL_PARAPHRASE'},
        fact_rephrases:[{fact_id:'python',text:'Reviewed proposed wording',reviewed:true,review_policy:'USER_CONFIRMED_FACTUAL_PARAPHRASE'}]},
      facts_used:[{fact_id:'python',kind:'responsibility',text:'Original source wording',source:'Original approved record',confidence:100,verified:true,user_approved:true}]};
    const html=renderToStaticMarkup(<DocumentHistory documents={[resume]}/>);
    expect(html).toContain('AI-assisted resume wording');
    expect(html).toContain('synthetic-provider');expect(html).toContain('fixture-model');
    expect(html).toContain('deterministic-reviewed-renderer');
    expect(html).toContain('Original source wording');expect(html).toContain('Original approved record');
    expect(html).toContain('Reviewed proposed wording');
    expect(html).toContain('automatic checks cannot verify every claim');
    const corrupt=renderToStaticMarkup(<DocumentHistory documents={[{...resume,preview_error:'Resume validation failed.'}]}/>);
    expect(corrupt).not.toContain('Provider: synthetic-provider');
    const letter=renderToStaticMarkup(<DocumentHistory documents={[{...resume,kind:'cover-letter'}]}/>);
    expect(letter).not.toContain('AI-assisted resume wording');
  });
});

const savedAudit:SavedResumeQualityAudit={
  schema_version:1,method:'CASEFOLD_LITERAL_TERMS_V1',status:'AVAILABLE',reason:'RECORDED_TARGET_TERMS',
  factual_validation:'VALIDATED_SAVED_SOURCES',
  source:{document_id:'saved-resume',document_version:2,profile_version:'saved-profile',text_sha256:'a'.repeat(64),
    target_fingerprint:'b'.repeat(64),target_terms_sha256:'c'.repeat(64),job_id:'recorded-job'},
  present_terms:['Python','SQL'],missing_terms:['Kubernetes'],term_count:3,
};

function savedResume(overrides:Partial<GeneratedDocument>={}):GeneratedDocument {
  return {...historical,id:'saved-resume',version:2,profile_version:'saved-profile',job_id:'recorded-job',
    files:{txt:'resume.txt'},sha256:{txt:'a'.repeat(64)},missing_fact_ids:[],
    preview_text:'Reviewed saved résumé\nPython and SQL services.',preview_error:null,avoid_list_error:null,
    generation_context:{mode:'APPROVED_SKILL_ORDER',job_id:'recorded-job',job_title:'Recorded target role',
      job_skills:['Python',' SQL ','Kubernetes','python'],target_fingerprint:'b'.repeat(64)},
    quality_audit:{...savedAudit,source:{...savedAudit.source!}},...overrides};
}

function savedResumeMarkup(document:GeneratedDocument):string {
  return renderToStaticMarkup(<DocumentHistory documents={[document]}/>);
}

function malformedAuditMarkup(audit:unknown):string {
  // Exercise malformed network payloads despite the declared TypeScript contract.
  return savedResumeMarkup(savedResume({quality_audit:audit as SavedResumeQualityAudit}));
}

describe('saved résumé keyword check',()=>{
  it('shows saved-revision term appearances separately from qualification and source validation',()=>{
    const html=savedResumeMarkup(savedResume());
    expect(html).toContain('Saved résumé keyword check');
    expect(html).toContain('Terms found in this saved revision');
    expect(html).toContain('Terms absent from this saved revision');
    expect(html).toContain('>Python</li>');expect(html).toContain('>SQL</li>');expect(html).toContain('>Kubernetes</li>');
    expect(html).toContain('recorded target snapshot');expect(html).toContain('case-insensitive literal term appearance');
    expect(html).toContain('Saved document and source validation succeeded');
    expect(html).toContain('Term appearance alone does not establish qualifications');
    expect(html).toContain('Human review is still needed to assess factual accuracy and job fit');
    expect(html).not.toContain('ATS score');expect(html).not.toContain('100%');expect(html).not.toContain('Regenerate');
    expect(html.slice(0,html.indexOf('Generation details and facts'))).not.toContain('a'.repeat(64));
    expect(html).not.toContain('b'.repeat(64));expect(html).not.toContain('c'.repeat(64));
    expect(html).toContain('Download TXT');
  });

  it('keeps legacy or missing audit evidence unavailable without inventing an empty successful result',()=>{
    for(const document of [historical,savedResume({quality_audit:undefined}),savedResume({quality_audit:null})]){
      const html=savedResumeMarkup(document);
      expect(html).toContain('Keyword check unavailable for this saved revision');
      expect(html).not.toContain('Terms found in this saved revision');
      expect(html).not.toContain('Saved document and source validation succeeded');
    }
    expect(savedResumeMarkup(savedResume({quality_audit:undefined}))).toContain('Preview validated resume');
  });

  it.each([
    ['document_id','another-document'],['document_version',3],['profile_version','other-profile-snapshot'],
    ['text_sha256','d'.repeat(64)],['target_fingerprint','e'.repeat(64)],['job_id','other-job'],
    ['target_terms_sha256','invalid-checksum'],
  ])('withholds a result with mismatched or malformed source field %s',(field,value)=>{
    const html=malformedAuditMarkup({...savedAudit,source:{...savedAudit.source,[field]:value}});
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
    expect(html).not.toContain('Saved document and source validation succeeded');
  });

  it.each([
    {preview_error:'Resume validation failed.'},
    {avoid_list_error:'Content blocked by the current avoid list.'},
    {responsibility_context_valid:false},
    {preview_text:null},
    {preview_text:undefined},
    {preview_text:' \n\t '},
    {preview_error:undefined},
  ])('withholds a successful audit without an explicitly validated usable preview (%j)',overrides=>{
    const html=savedResumeMarkup(savedResume(overrides));
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
    expect(html).not.toContain('Saved document and source validation succeeded');
  });

  it.each([
    {schema_version:2},{schema_version:true},{method:'ANOTHER_METHOD'},
    {status:'UNKNOWN'},{reason:'DOCUMENT_NOT_VALIDATED'},{factual_validation:'NOT_VALIDATED'},
    {source:null},{term_count:0},{term_count:2},{term_count:3.5},{term_count:'3'},
    {present_terms:['Python','SQL','Python'],term_count:4},
    {present_terms:['Python','SQL'],missing_terms:['python']},
    {present_terms:['Python','Foreign term']},
    {present_terms:['Python','']},
    {present_terms:['Python',{term:'SQL'}]},
    {present_terms:null},{missing_terms:'Kubernetes'},
    {present_terms:['Python'],missing_terms:['SQL'],term_count:2},
    {present_terms:['Python',' SQL ']},
  ])('rejects an inconsistent audit response (%j)',changes=>{
    const html=malformedAuditMarkup({...savedAudit,...changes});
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
    expect(html).not.toContain('Saved document and source validation succeeded');
  });

  it('requires the recorded snapshot rather than substituting unbound or current job terms',()=>{
    const recorded=savedResume();
    for(const changes of [
      {generation_context:null},
      {generation_context:{mode:'APPROVED_SKILL_ORDER'}},
      {generation_context:{...recorded.generation_context!,target_fingerprint:undefined}},
      {generation_context:{...recorded.generation_context!,job_skills:['Different current job term']}},
      {job_id:undefined},
      {sha256:{pdf:'a'.repeat(64)}},
    ]){
      const html=savedResumeMarkup(savedResume(changes));
      expect(html).toContain('Keyword check unavailable');
      expect(html).not.toContain('Terms found in this saved revision');
    }
  });

  it('shows a complete international target partition without recomputing term matching',()=>{
    const document=savedResume();
    document.generation_context={...document.generation_context!,job_skills:['  Straße\t','Python','École','école']};
    document.quality_audit={...savedAudit,present_terms:['Straße','École'],missing_terms:['Python'],term_count:3};
    const html=savedResumeMarkup(document);
    expect(html).toContain('Terms found in this saved revision');
    expect(html).toContain('>Straße</li>');expect(html).toContain('>École</li>');
    expect(html).toContain('Terms absent from this saved revision');expect(html).toContain('>Python</li>');
    expect(html).not.toContain('Keyword check unavailable');
  });

  it('withholds an ambiguous full-casefold partition instead of claiming no terms are missing',()=>{
    const document=savedResume();
    document.generation_context={...document.generation_context!,job_skills:['Straße','STRASSE']};
    document.quality_audit={...savedAudit,present_terms:['Straße'],missing_terms:[],term_count:1};
    const html=savedResumeMarkup(document);
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('No recorded target terms are absent');
    expect(html).not.toContain('Saved document and source validation succeeded');
  });

  it.each([
    {label:'omitted unrelated term',snapshot:['Straße','Python'],present_terms:['Straße'],missing_terms:[],term_count:1},
    {label:'duplicate Unicode case variant',snapshot:['Straße','École','école'],present_terms:['Straße','École'],missing_terms:['école'],term_count:3},
    {label:'term split across found and absent lists',snapshot:['Straße','Python'],present_terms:['Straße'],missing_terms:['Straße','Python'],term_count:3},
    {label:'changed first recorded spelling',snapshot:['Straße','École','école'],present_terms:['Straße','école'],missing_terms:[],term_count:2},
  ])('rejects an incomplete or contradictory mixed Unicode partition: $label',({snapshot,present_terms,missing_terms,term_count})=>{
    const document=savedResume();
    document.generation_context={...document.generation_context!,job_skills:snapshot};
    document.quality_audit={...savedAudit,present_terms,missing_terms,term_count};
    const html=savedResumeMarkup(document);
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
    expect(html).not.toContain('No recorded target terms are absent');
  });

  it.each([
    {mode:undefined},{mode:'APPROVED_CONTACT_HEADER'},{job_id:undefined},{job_id:'different-target'},
  ])('rejects missing or mismatched recorded target identity (%j)',changes=>{
    const document=savedResume();
    document.generation_context={...document.generation_context,...changes} as NonNullable<GeneratedDocument['generation_context']>;
    const html=savedResumeMarkup(document);
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
  });

  it.each(['Py\u200bthon','\u2066Python\u2069'])('rejects invisible format controls in recorded or reported terms (%s)',term=>{
    const document=savedResume();
    document.generation_context={...document.generation_context!,job_skills:[term,'SQL','Kubernetes']};
    document.quality_audit={...savedAudit,present_terms:[term,'SQL']};
    const html=savedResumeMarkup(document);
    expect(html).toContain('Keyword check unavailable');
    expect(html).not.toContain('Terms found in this saved revision');
  });

  it('escapes recorded terms in both the found and absent lists',()=>{
    const found='<img src=x onerror=alert(1)>';const absent='<script>example</script>';
    const document=savedResume({preview_text:`Saved text ${found}`});
    document.generation_context={...document.generation_context!,job_skills:[found,absent]};
    document.quality_audit={...savedAudit,present_terms:[found],missing_terms:[absent],term_count:2};
    const html=savedResumeMarkup(document);
    expect(html).toContain('Terms found in this saved revision');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).toContain('&lt;script&gt;example&lt;/script&gt;');
    expect(html).not.toContain('<img');expect(html).not.toContain('<script>');
  });

  it('describes a zero-match result as absent terms without implying failed factual validation',()=>{
    const html=savedResumeMarkup(savedResume({preview_text:'Reviewed saved wording.',quality_audit:{...savedAudit,
      present_terms:[],missing_terms:['Python','SQL','Kubernetes']}}));
    expect(html).toContain('None of the recorded target terms appears');
    expect(html).toContain('Saved document and source validation succeeded');
    expect(html).not.toContain('Keyword check unavailable');
  });

  it('shows a safe missing-target explanation and never echoes unknown backend reason text',()=>{
    const unavailable={...savedAudit,status:'UNAVAILABLE',reason:'TARGET_SNAPSHOT_MISSING',source:null,
      present_terms:[],missing_terms:[],term_count:0};
    const html=malformedAuditMarkup(unavailable);
    expect(html).toContain('This revision has no recorded target snapshot');
    expect(html).not.toContain('Terms found in this saved revision');
    const unknown=malformedAuditMarkup({...unavailable,reason:'private-reason-canary'});
    expect(unknown).toContain('Keyword check unavailable');expect(unknown).not.toContain('private-reason-canary');
  });

  it('does not present the resume-only audit on a cover letter or answer export',()=>{
    for(const kind of ['cover-letter','application-answer-export']){
      const html=savedResumeMarkup(savedResume({kind}));
      expect(html).not.toContain('Saved résumé keyword check');
    }
  });
});
