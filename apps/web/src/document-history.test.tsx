import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { DocumentHistory, type GeneratedDocument } from './document-history';

const historical:GeneratedDocument={id:'old',kind:'resume',version:1,profile_version:'snapshot-1',fact_ids:['python'],
  files:{pdf:'resume.pdf'},sha256:{pdf:'abc'},template:'verified-facts-v1',created_at:null,generation_model:null,
  facts_used:[],missing_fact_ids:['python']};
describe('document provenance',()=>{
  it('renders a contact-only base contract without skill-order fields',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,template:'approved-contact-base-v1',generation_model:'deterministic-contact-copy-v1',generation_context:{mode:'APPROVED_CONTACT_HEADER',contact_header:['Alex','Phone: 555 0100']},missing_fact_ids:[],facts_used:[{fact_id:'python',kind:'skill',text:'Python',source:'Review',confidence:100,verified:true,user_approved:true}]}]}/>);
    expect(html).toContain('Saved contact details included');expect(html).toContain('Phone: 555 0100');expect(html).not.toContain('Tailored resume:');
  });
  it('describes explicit omissions without claiming all work history was retained',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[{...historical,template:'approved-fact-selection-headings-v1',generation_model:'deterministic-fact-selection-v1',generation_context:{mode:'APPROVED_SKILL_ORDER',job_title:'Example role',job_skills:[],matched_skills:{},original_fact_ids:['python'],ordered_fact_ids:['python'],selection_mode:'EXPLICIT_APPROVED_FACTS',selected_fact_ids:['python'],omitted_fact_ids:['sql']}}]}/>);
    expect(html).toContain('Tailored resume: selected facts');expect(html).toContain('Approved facts included: 1. Omitted from this resume: 1.');expect(html).not.toContain('work history preserved');
  });
  it('shows missing historical evidence without invented timestamps or dead format links',()=>{
    const html=renderToStaticMarkup(<DocumentHistory documents={[historical]}/>);
    expect(html).toContain('Not recorded for this historical document');
    expect(html).toContain('Some original facts are unavailable: python');
    expect(html).toContain('Download PDF');
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
});
