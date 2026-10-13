import {describe, expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {MockDynamicReviewView} from './mock-dynamic-review';
const snapshot={ready:true,fingerprint:'a'.repeat(64),fields:[{question_id:'region',label:'State or province',value:'Oklahoma'}],blockers:[],latest_review:null};
const base={snapshot,checked:false,busy:false,disabled:false,error:'',notice:'',onChecked:()=>{},onSave:()=>{},onRefresh:()=>{}};
const render=(change:Partial<Parameters<typeof MockDynamicReviewView>[0]>={})=>renderToStaticMarkup(<MockDynamicReviewView {...base} {...change}/>);
describe('conditional dry-run review',()=>{
  it('requires explicit consent for the current displayed values',()=>{
    expect(render()).toContain('Oklahoma');
    expect(render()).toContain('<button type="button" disabled="">Save conditional-field review</button>');
    expect(render({checked:true})).toContain('<button type="button">Save conditional-field review</button>');
    expect(render()).toContain('dry-run only');
  });
  it('cannot approve while unavailable, blocked or another operation is active',()=>{
    expect(render({snapshot:null,error:'Refresh failed'})).not.toContain('Save conditional-field review');
    for(const change of [{busy:true},{disabled:true},{snapshot:{...snapshot,ready:false}}]) {
      expect(render({...change,checked:true})).toContain('<button type="button" disabled="">Save conditional-field review</button>');
    }
  });
  it('renders answer text without interpreting markup',()=>{
    const html=render({snapshot:{...snapshot,fields:[{question_id:'x',label:'Question',value:'<script>attack</script>'}]}});
    expect(html).not.toContain('<script>');expect(html).toContain('&lt;script&gt;');
  });
  it('shows saved review only for the displayed fingerprint and keeps execution separate',()=>{
    expect(render({snapshot:{...snapshot,latest_review:{fingerprint:snapshot.fingerprint}}})).toContain('Conditional review saved');
    expect(render({snapshot:{...snapshot,latest_review:{fingerprint:'old'}}})).not.toContain('Conditional review saved');
    expect(render()).not.toContain('Run dry-run');
  });
});
