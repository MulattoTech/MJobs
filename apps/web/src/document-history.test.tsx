import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { coverLetterLineDiff, DocumentHistory, type GeneratedDocument } from './document-history';

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
