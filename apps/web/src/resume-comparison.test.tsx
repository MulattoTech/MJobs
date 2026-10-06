import {describe,expect,it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {ComparisonResult,ResumeComparison,type Comparison} from './resume-comparison';
import type {GeneratedDocument} from './document-history';

describe('resume comparison',()=>{
  it('offers legacy unknown-model and new exact-contract baselines without accepting mismatched models',()=>{
    const base:GeneratedDocument={id:'legacy',profile_id:'fixture',kind:'resume',version:1,profile_version:'snapshot',fact_ids:[],files:{},sha256:{},template:'verified-facts-v1',created_at:null,generation_model:null,facts_used:[],missing_fact_ids:[]};
    const target:GeneratedDocument={...base,id:'target',version:3,template:'approved-skill-order-headings-v1',generation_model:'deterministic-skill-order-v2',generation_context:{mode:'APPROVED_SKILL_ORDER',job_title:'Example',job_skills:[],matched_skills:{},original_fact_ids:[],ordered_fact_ids:[]}};
    const bulletTarget:GeneratedDocument={...target,id:'bullet-target',version:4,template:'approved-skill-and-bullet-emphasis-v2',generation_model:'deterministic-skill-and-bullet-emphasis-v1'};
    const selectedBulletTarget:GeneratedDocument={...target,id:'selected-bullet-target',version:5,template:'approved-selection-and-bullet-emphasis-v2',generation_model:'deterministic-fact-selection-and-bullet-emphasis-v1'};
    const html=renderToStaticMarkup(<ResumeComparison documents={[base,{...base,id:'new',version:2,template:'verified-headings-v1',generation_model:'deterministic-fact-copy-v2'},target,bulletTarget,selectedBulletTarget,{...base,id:'invalid',template:'verified-headings-v1'}]}/>);
    expect(html).toContain('value="g:legacy"');expect(html).toContain('value="g:new"');expect(html).toContain('value="target"');expect(html).toContain('value="bullet-target"');expect(html).toContain('value="selected-bullet-target"');expect(html).not.toContain('g:invalid');
  });
  it('renders every change category, actual revision labels and escaped historical text',()=>{
    const before={fact_id:'summary',kind:'summary',text:'Original summary'};
    const after={...before,text:'<script>reviewed text</script>'};
    const result:Comparison={base:{id:'a',version:1,text:'Original'},tailored:{id:'b',version:2,text:after.text},
      summary_changes:[{fact_id:'summary',before,after}],bullet_changes:[],
      reordered:[{fact_id:'python',text:'Python',before_position:2,after_position:1}],
      emphasized_skills:[{fact_id:'python',kind:'skill',text:'Python',job_skills:['Python']}],
      omitted:[{fact_id:'old',kind:'certification',text:'Original certificate'}],added:[],contact_changed:false,scope:'Recorded revisions only'};
    const html=renderToStaticMarkup(<ComparisonResult result={result}/>);
    for(const title of ['Summary changes','Bullet and fact changes','Reordering','Skills and bullets emphasized','Content omitted','Generated base · v1','Tailored resume · v2'])expect(html).toContain(title);
    expect(html).toContain('Original certificate');expect(html).toContain('Original summary');
    expect(html).toContain('fact position 2 → 1');expect(html).not.toContain('<script>');
  });
  it('shows OCR uncertainty and unknown sections instead of invented unchanged results',()=>{
    const result:Comparison={base:{id:'upload',version:2,text:'Uncertain original',source_kind:'UPLOADED',filename:'scan.pdf',format:'pdf',sha256:'abc',profile_id:'fixture',extraction:{status:'READY_FOR_REVIEW',warnings:['OCR text may contain mistakes.'],used_ocr:true}},tailored:{id:'target',version:3,text:'Approved output'},
      summary_changes:[],bullet_changes:[],reordered:[],emphasized_skills:[],omitted:[],added:[],contact_changed:null,scope:'Extracted text only',section_availability:{summary_changes:false,bullet_changes:false}};
    const html=renderToStaticMarkup(<ComparisonResult result={result}/>);
    expect(html).toContain('OCR text is uncertain');expect(html).toContain('OCR text may contain mistakes.');
    expect(html).toContain('Source section unavailable or ambiguous');expect(html).not.toContain('No changes.');
    expect(html).toContain('Download compared original');expect(html).toContain('Uploaded base · v2');
  });
});
