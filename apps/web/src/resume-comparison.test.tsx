import {describe,expect,it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {ComparisonResult,type Comparison} from './resume-comparison';

describe('resume comparison',()=>{
  it('renders every change category, actual revision labels and escaped historical text',()=>{
    const before={fact_id:'summary',kind:'summary',text:'Original summary'};
    const after={...before,text:'<script>reviewed text</script>'};
    const result:Comparison={base:{id:'a',version:1,text:'Original'},tailored:{id:'b',version:2,text:after.text},
      summary_changes:[{fact_id:'summary',before,after}],bullet_changes:[],
      reordered:[{fact_id:'python',text:'Python',before_position:2,after_position:1}],
      emphasized_skills:[{fact_id:'python',kind:'skill',text:'Python',job_skills:['Python']}],
      omitted:[{fact_id:'old',kind:'certification',text:'Original certificate'}],added:[],contact_changed:false,scope:'Recorded revisions only'};
    const html=renderToStaticMarkup(<ComparisonResult result={result}/>);
    for(const title of ['Summary changes','Bullet and fact changes','Reordering','Skills emphasized','Content omitted','Generated base · v1','Tailored resume · v2'])expect(html).toContain(title);
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
