import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it,vi} from 'vitest';
import {
  ApplicationMultipageWorkspace,
  createApplicationMultipageClient,
  type ApplicationMultipageState,
} from './application-multipage-workspace';
import type {ApplicationMultipagePage} from './application-multipage-review';

const pages:ApplicationMultipagePage[]=[
  {id:'contact',title:'Contact information',fields:[{id:'email',label:'Email',required:true}]},
  {id:'experience',title:'Experience',fields:[{id:'summary',label:'Experience summary'}]},
];

const state:ApplicationMultipageState={
  profile_id:'profile / one',application_id:'application & two',current_page_id:'contact',
  page_order:['contact','experience'],status:'IN_PROGRESS',progress:{contact:{email:'person@example.test'}},
  version:3,updated_at:'2026-10-09T12:00:00Z',manual_intervention_required:true,
};

describe('application multipage workspace',()=>{
  it('loads saved state from the encoded profile and application path',async()=>{
    const fetcher=vi.fn<typeof fetch>().mockResolvedValue(Response.json(state));
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>true);

    await expect(client.load(state.profile_id,state.application_id)).resolves.toEqual(state);
    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%20%2F%20one/applications/application%20%26%20two/multipage-state',
      expect.objectContaining({method:'GET',cache:'no-store'}),
    );
  });

  it('rejects a successful response bound to a different profile or application',async()=>{
    const mismatches=[
      {...state,profile_id:'profile-b'},
      {...state,application_id:'application-b'},
    ];
    const fetcher=vi.fn<typeof fetch>();
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>true);

    for(const response of mismatches) {
      fetcher.mockResolvedValueOnce(Response.json(response));
      await expect(client.load(state.profile_id,state.application_id)).rejects.toMatchObject({code:'SCOPE_MISMATCH'});
    }
  });

  it('passes the typed transition body to the encoded profile and application endpoint',async()=>{
    const saved={...state,current_page_id:'experience',progress:{...state.progress,experience:{summary:'User draft'}},version:4};
    const fetcher=vi.fn<typeof fetch>().mockResolvedValue(Response.json(saved));
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>true);
    const request={request_id:'transition-1',action:'NEXT' as const,expected_version:3,page_id:'contact',
      next_page_id:'experience',progress:saved.progress};

    await expect(client.transition(state.profile_id,state.application_id,request)).resolves.toEqual(saved);
    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%20%2F%20one/applications/application%20%26%20two/multipage-state',
      expect.objectContaining({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)}),
    );
  });

  it('does not let the typed client issue a request outside its selected scope',async()=>{
    const fetcher=vi.fn<typeof fetch>();
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>true);

    await expect(client.load('profile-other',state.application_id)).rejects.toMatchObject({code:'STALE_SCOPE'});
    await expect(client.transition(state.profile_id,'application-other',{
      request_id:'transition-other',action:'SAVE_PROGRESS',expected_version:3,page_id:'contact',
      progress:state.progress,
    })).rejects.toMatchObject({code:'STALE_SCOPE'});
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('discards a response after the selected profile or application changes',async()=>{
    let active=true;
    let resolveResponse!:(response:Response)=>void;
    const fetcher=vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>(resolve=>{resolveResponse=resolve;}));
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>active);
    const pending=client.load(state.profile_id,state.application_id);
    active=false;
    resolveResponse(Response.json(state));

    await expect(pending).rejects.toMatchObject({code:'STALE_SCOPE'});
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('discards an in-flight response after workspace unmount aborts its request',async()=>{
    const controller=new AbortController();
    let active=true;
    let resolveResponse!:(response:Response)=>void;
    const fetcher=vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>(resolve=>{resolveResponse=resolve;}));
    const client=createApplicationMultipageClient(state.profile_id,state.application_id,pages,fetcher,()=>active,controller.signal);
    const pending=client.load(state.profile_id,state.application_id);
    active=false;
    controller.abort();
    resolveResponse(Response.json(state));

    await expect(pending).rejects.toMatchObject({code:'STALE_SCOPE'});
    expect(fetcher.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it('asks for a selected profile and application before rendering a saved workspace',()=>{
    const html=renderToStaticMarkup(<ApplicationMultipageWorkspace profileId={null} applicationId={null} pages={pages}/>);
    expect(html).toContain('Choose a profile and application');
    expect(html).not.toContain('person@example.test');
  });
});
