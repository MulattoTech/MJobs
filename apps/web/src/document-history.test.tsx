import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { DocumentHistory, type GeneratedDocument } from './document-history';

const historical:GeneratedDocument={id:'old',kind:'resume',version:1,profile_version:'snapshot-1',fact_ids:['python'],
  files:{pdf:'resume.pdf'},sha256:{pdf:'abc'},template:'verified-facts-v1',created_at:null,generation_model:null,
  facts_used:[],missing_fact_ids:['python']};
describe('document provenance',()=>{
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
});
