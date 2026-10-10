import {describe,expect,it,vi} from 'vitest';
import {cancelMockRun,forgetTerminalMockRun,loadMockRunIntent,lookupMockRun,mockRunEndpoint,mockRunStorageKey,
  postMockRun,resumeMockDryRun,saveMockRunIntent,type IntentStorage,type MockRunIntent,type MockRunReceipt} from './mock-run-client';
const scope={profileId:'fixture',applicationId:'app-1'};
const intent:MockRunIntent={version:1,requestId:'reviewed-run-1',submit:false};
const queued:MockRunReceipt={operation_id:'operation-1',state:'QUEUED',error_code:null,replayed:false,
  input_fingerprint:'a'.repeat(64),external_submission_enabled:false};
function storage():IntentStorage {
  const values=new Map<string,string>();
  return {getItem:key=>values.get(key)??null,setItem:(key,value)=>{values.set(key,value);},removeItem:key=>{values.delete(key);}};
}
function request(value:unknown=queued,status=200) {
  return vi.fn(async()=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}})) as unknown as ReturnType<typeof vi.fn>&typeof fetch;
}

describe('durable mock run client',()=>{
  it('persists exactly the opaque intent and reloads it without consent or documents',()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    expect(loadMockRunIntent(local,scope)).toEqual(intent);
    expect(JSON.parse(local.getItem(mockRunStorageKey(scope))!)).toEqual({version:1,requestId:'reviewed-run-1',submit:false});
    expect(loadMockRunIntent(local,{...scope,profileId:'other'})).toBeNull();
    expect(loadMockRunIntent(local,{...scope,applicationId:'app-2'})).toBeNull();
  });
  it('encodes both profile and application boundaries',()=>{
    expect(mockRunEndpoint({profileId:'a/b',applicationId:'c?d'})).toBe('/profiles/a%2Fb/applications/c%3Fd/mock-run');
    expect(mockRunStorageKey({profileId:'a:b',applicationId:'c'})).not.toBe(mockRunStorageKey({profileId:'a',applicationId:'b:c'}));
  });
  it('does not silently discard malformed or foreign-version pending intent',()=>{
    for(const value of ['bad json','null','[]',JSON.stringify({...intent,version:2}),JSON.stringify({...intent,requestId:'../bad'}),JSON.stringify({...intent,submit:'true'}),JSON.stringify({...intent,consent:true})]) {
      const local=storage();local.setItem(mockRunStorageKey(scope),value);
      expect(()=>loadMockRunIntent(local,scope)).toThrow();
      expect(local.getItem(mockRunStorageKey(scope))).toBe(value);
    }
  });
  it('storage rejection stops persistence rather than inventing a recoverable identity',()=>{
    const local={...storage(),setItem:()=>{throw new Error('quota');}};
    expect(()=>saveMockRunIntent(local,scope,intent)).toThrow('quota');
  });
  it('status recovery is a scoped GET and never a POST',async()=>{
    const send=request();expect(await lookupMockRun(scope,intent,send)).toEqual(queued);
    expect(send).toHaveBeenCalledExactlyOnceWith('/profiles/fixture/applications/app-1/mock-runs/reviewed-run-1',{method:'GET'});
  });
  it('a missing acknowledgement is explicit and keeps the pending intent',async()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    expect(await lookupMockRun(scope,intent,request({},404))).toBeNull();
    expect(loadMockRunIntent(local,scope)).toEqual(intent);
  });
  it('network loss followed by resend retains exactly the same request body',async()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    const lost=vi.fn(async()=>{throw new TypeError('network lost');}) as unknown as typeof fetch;
    await expect(postMockRun(scope,intent,lost)).rejects.toThrow('network lost');
    const retry=request({...queued,replayed:true});
    expect((await postMockRun(scope,loadMockRunIntent(local,scope)!,retry)).replayed).toBe(true);
    expect(retry.mock.calls[0][1]).toEqual({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:intent.requestId,submit:false})});
  });
  it('never forgets an unresolved or still-running request',()=>{
    for(const result of [null,queued,{...queued,state:'RUNNING'}]) {
      const local=storage();saveMockRunIntent(local,scope,intent);
      expect(()=>forgetTerminalMockRun(local,scope,result)).toThrow('Resolve');
      expect(loadMockRunIntent(local,scope)).toEqual(intent);
    }
  });
  it('only explicitly acknowledged terminal state permits a fresh reviewed intent',()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    forgetTerminalMockRun(local,scope,{...queued,state:'CANCELLED'});
    expect(loadMockRunIntent(local,scope)).toBeNull();
  });
  it('rejects unsupported response states, fingerprints and external-enabled flags',async()=>{
    for(const delta of [{state:'APPROVED_BY_AI'},{input_fingerprint:'unknown'},{external_submission_enabled:true},{operation_id:'../bad'},{replayed:undefined}])
      await expect(postMockRun(scope,intent,request({...queued,...delta}))).rejects.toThrow('could not be verified');
  });
  it('shows only bounded fixed error codes, never arbitrary provider diagnostics',async()=>{
    await expect(postMockRun(scope,intent,request({detail:{code:'APPLICATION_RUN_REQUEST_REUSED'}},409))).rejects.toThrow('APPLICATION_RUN_REQUEST_REUSED');
    await expect(postMockRun(scope,intent,request({detail:'private credential'},500))).rejects.toThrow('Mock run request failed (500)');
  });
  it('cancels only the acknowledged operation and surfaces a reconciliation conflict',async()=>{
    const send=request({state:'CANCELLED'});await cancelMockRun('operation-1',send);
    expect(send).toHaveBeenCalledExactlyOnceWith('/operations/operation-1/cancel',{method:'POST'});
    await expect(cancelMockRun('operation-1',request({detail:{code:'RECONCILIATION_REQUIRED'}},409))).rejects.toThrow('RECONCILIATION_REQUIRED');
    await expect(cancelMockRun('../bad',send)).rejects.toThrow('Invalid');
  });
});

describe('separate non-submitting manual-auth resume',()=>{
  it('saves a new dry-run intent before posting',async()=>{
    const local=storage(),send=request();
    send.mockImplementation(async()=>{
      expect(loadMockRunIntent(local,scope)).toEqual({version:1,requestId:'manual-new',submit:false});
      return new Response(JSON.stringify(queued),{status:202});
    });
    await resumeMockDryRun(scope,local,send,()=>'manual-new');
    expect(send.mock.calls[0][1]).toMatchObject({method:'POST',body:JSON.stringify({request_id:'manual-new',submit:false})});
  });
  it('replaces only an acknowledged terminal auth intent and never inherits submit mode',async()=>{
    for(const submit of [false,true]) {
      const local=storage();saveMockRunIntent(local,scope,{...intent,submit});
      const send=request();
      send.mockResolvedValueOnce(new Response(JSON.stringify({...queued,state:'BLOCKED',error_code:'AUTH_REQUIRED'})));
      await resumeMockDryRun(scope,local,send,()=>'manual-new');
      expect(send.mock.calls).toHaveLength(2);
      expect(send.mock.calls[1][1]).toMatchObject({method:'POST',body:JSON.stringify({request_id:'manual-new',submit:false})});
      expect(loadMockRunIntent(local,scope)?.requestId).toBe('manual-new');
    }
  });
  it.each(['QUEUED','RUNNING','COMPLETED'])('recovers %s without posting or changing identity',async(state)=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    const send=request({...queued,state});
    expect((await resumeMockDryRun(scope,local,send,()=>{throw new Error('new ID allocated');})).state).toBe(state);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][1]).toEqual({method:'GET'});
    expect(loadMockRunIntent(local,scope)).toEqual(intent);
  });
  it('reuses the identical dry-run request when admission is not yet found',async()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    const send=request();send.mockResolvedValueOnce(new Response('{}',{status:404}));
    await resumeMockDryRun(scope,local,send,()=>{throw new Error('new ID allocated');});
    expect(send.mock.calls[1][1]).toMatchObject({method:'POST',body:JSON.stringify({request_id:intent.requestId,submit:false})});
  });
  it('does not replace an unknown pending intent during a failed lookup',async()=>{
    const local=storage();saveMockRunIntent(local,scope,intent);
    const send=request();send.mockRejectedValueOnce(new TypeError('network lost'));
    await expect(resumeMockDryRun(scope,local,send)).rejects.toThrow('network lost');
    expect(send).toHaveBeenCalledTimes(1);expect(loadMockRunIntent(local,scope)).toEqual(intent);
  });
  it('never replays an unresolved submission intent from the non-submitting resume button',async()=>{
    for(const missing of [false,true]) {
      const local=storage();saveMockRunIntent(local,scope,{...intent,submit:true});
      const send=missing?request({},404):request(queued);
      await expect(resumeMockDryRun(scope,local,send)).rejects.toThrow('Resolve the saved submission request');
      expect(send).toHaveBeenCalledTimes(1);
      expect(send.mock.calls[0][1]).toEqual({method:'GET'});
      expect(loadMockRunIntent(local,scope)?.submit).toBe(true);
    }
  });
  it('does not send the request when saving the new intent fails',async()=>{
    const local={...storage(),setItem:()=>{throw new Error('quota');}},send=request();
    await expect(resumeMockDryRun(scope,local,send)).rejects.toThrow('quota');
    expect(send).not.toHaveBeenCalled();
  });
});
