import {describe,expect,it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {MockRunControlsView} from './mock-run-controls';
import type {MockRunIntent,MockRunReceipt} from './mock-run-client';
const intent:MockRunIntent={version:1,requestId:'reviewed-run-1',submit:false};
const receipt:MockRunReceipt={operation_id:'operation-1',state:'QUEUED',error_code:null,replayed:false,
  input_fingerprint:'a'.repeat(64),external_submission_enabled:false};
const base={intent:null,receipt:null,busy:false,disabled:false,canStart:true,canCancel:true,consent:false,
  error:'',notice:'',storageBlocked:false,onConsent:()=>{},onBegin:()=>{},onLookup:()=>{},onResend:()=>{},onNew:()=>{},onCancel:()=>{}};
const render=(change:Partial<Parameters<typeof MockRunControlsView>[0]>={})=>renderToStaticMarkup(<MockRunControlsView {...base} {...change}/>);

describe('reviewed run recovery controls',()=>{
  it('explains route contention without treating restart or replay as new approval',()=>{
    const html=render({intent,receipt:{...receipt,state:'BLOCKED',error_code:'APPLICATION_ROUTE_IN_PROGRESS'}});
    expect(html).toContain('Another application run owns this route');
    expect(html).toContain('Restarting does not clear unfinished ownership');
    expect(html).toContain('Start a new reviewed run');
    expect(html).not.toContain('Resend same request');
    expect(render({intent,receipt:{...receipt,state:'BLOCKED',error_code:'APPLICATION_ROUTE_REVIEW_REQUIRED'}}))
      .toContain('unknown route ownership and needs review');
  });
  it('offers dry-run while fictional submission is gated by explicit consent',()=>{
    const html=render();expect(html).toContain('Run dry-run');
    expect(html).toContain('<button disabled="">Submit to local mock</button>');
    expect(render({consent:true})).toContain('<button>Submit to local mock</button>');
    expect(html).toContain('No real employer application is sent');
  });
  it('shows the retained request after missing acknowledgement without offering a competing new run',()=>{
    const html=render({intent,error:'Connection interrupted'});
    expect(html).toContain('reviewed-run-1');expect(html).toContain('Resend same request');
    expect(html).not.toContain('Start a new reviewed run');expect(html).not.toContain('>Run dry-run</button>');
    expect(html).toContain('The saved request has not been replaced');
  });
  it('does not restore persisted submission intent as renewed consent',()=>{
    expect(render({intent:{...intent,submit:true}})).toContain('<button disabled="">Resend same request</button>');
  });
  it('shows active status and cancellation but no same-request submission button',()=>{
    const html=render({intent,receipt});expect(html).toContain('QUEUED');expect(html).toContain('operation-1');
    expect(html).toContain('Cancel queued or running test');expect(html).not.toContain('Resend same request');
  });
  it('allows a new reviewed intent only after a terminal response',()=>{
    const html=render({intent,receipt:{...receipt,state:'CANCELLED'}});
    expect(html).toContain('Start a new reviewed run');expect(html).not.toContain('Cancel queued or running test');
  });
  it('does not permit another run after ambiguous or confirmed submission',()=>{
    const html=render({intent,receipt:{...receipt,state:'DEAD_LETTERED',error_code:'RECONCILIATION_REQUIRED'},canStart:false});
    expect(html).toContain('<button disabled="">Start a new reviewed run</button>');
    expect(html).toContain('check the existing receipt first');
  });
  it('renders untrusted display values as text and disables work after storage failure',()=>{
    const html=render({error:'<script>attack</script>',storageBlocked:true});
    expect(html).not.toContain('<script>');expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('<button disabled="">Run dry-run</button>');
  });
});

it('keeps cancellation available while filling but prevents a new run',()=>{
  const html=render({intent,receipt:{...receipt,state:'RUNNING'},canStart:false,canCancel:true});
  expect(html).toContain('<button>Cancel queued or running test</button>');
  expect(render({intent,receipt:{...receipt,state:'RUNNING'},canStart:false,canCancel:false}))
    .toContain('<button disabled="">Cancel queued or running test</button>');
});
