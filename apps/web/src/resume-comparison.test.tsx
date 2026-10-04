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
});
