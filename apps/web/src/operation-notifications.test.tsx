// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {fireEvent,getByRole,queryByRole} from '@testing-library/dom';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {OperationNotifications, type NotificationOperation} from './operation-notifications';

const operation:NotificationOperation={id:'one',kind:'PREPARE',state:'COMPLETED',updated_at:'2026-11-02T00:00:00Z'};
describe('configured in-app notifications',()=>{
  it('does not display notifications without opt-in',()=>{
    expect(renderToStaticMarkup(<OperationNotifications profileId="fixture" enabled={false} operations={[operation]}/>)).toBe('');
  });
  it('shows only bounded terminal outcomes using fixed, private-data-free copy',()=>{
    const rows=Array.from({length:9},(_,index)=>({...operation,id:String(index),kind:'secret-token-abc'}));
    const html=renderToStaticMarkup(<OperationNotifications profileId="fixture" enabled operations={rows}/>);
    expect(html.match(/Dismiss notification/g)).toHaveLength(5);
    expect(html).toContain('Background task completed');
    expect(html).not.toContain('secret-token-abc');
    expect(renderToStaticMarkup(<OperationNotifications profileId="fixture" enabled operations={[{...operation,state:'RUNNING'}]}/>)).toContain('No new activity');
  });
  it('supports dismissal, a new outcome, and muting without writing or sending',async()=>{
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>root.render(<OperationNotifications profileId="fixture" enabled operations={[operation]}/>));
      expect(container.textContent).toContain('Document preparation completed.');
      await act(async()=>Array.from(container.querySelectorAll('button')).find(b=>b.textContent==='Dismiss notification')!.click());
      expect(container.textContent).toContain('No new activity');
      await act(async()=>root.render(<OperationNotifications profileId="fixture" enabled operations={[{...operation,state:'FAILED'}]}/>));
      expect(container.textContent).toContain('needs attention');
      await act(async()=>Array.from(container.querySelectorAll('button')).find(b=>b.textContent==='Mute for this visit')!.click());
      expect(container.textContent).not.toContain('needs attention');
      await act(async()=>root.render(<OperationNotifications profileId="fixture" enabled={false} operations={[operation]}/>));
      expect(container.textContent).toBe('');
    } finally {await act(async()=>root.unmount());container.remove();}
  });
  it('keeps dismissals after reload, scopes them per profile, and reveals changed outcomes',async()=>{
    localStorage.clear();
    const container=document.createElement('div');document.body.append(container);
    let root=createRoot(container);
    const render=async(profileId:string,entry=operation)=>{
      await act(async()=>root.render(<OperationNotifications key={profileId} profileId={profileId} enabled operations={[entry]}/>));
    };
    try {
      await render('profile-alpha');
      expect(container.textContent).toContain('Document preparation completed.');
      await act(async()=>Array.from(container.querySelectorAll('button')).find(b=>b.textContent==='Dismiss notification')!.click());
      expect(container.textContent).toContain('No new activity');
      await act(async()=>root.unmount());
      root=createRoot(container);
      await render('profile-alpha');
      expect(container.textContent).toContain('No new activity');
      await render('profile-alpha',{...operation,state:'FAILED'});
      expect(container.textContent).toContain('needs attention');
      await render('profile-beta');
      expect(container.textContent).toContain('Document preparation completed.');
      const saved=JSON.parse(localStorage.getItem('mjobs-notification-dismissals:v1:profile-alpha')||'[]') as string[];
      expect(saved).toHaveLength(1);
      expect(saved[0]).toContain('one');
    } finally {await act(async()=>root.unmount());container.remove();localStorage.clear();}
  });
  it('ignores corrupt or oversized browser data and bounds persisted metadata',async()=>{
    const storageKey='mjobs-notification-dismissals:v1:profile-corrupt';
    localStorage.setItem(storageKey,'{invalid');
    const container=document.createElement('div');document.body.append(container);
    let root=createRoot(container);
    const render=async()=>await act(async()=>root.render(
      <OperationNotifications profileId="profile-corrupt" enabled operations={[operation]}/>));
    const dismiss=async()=>await act(async()=>Array.from(container.querySelectorAll('button'))
      .find(button=>button.textContent==='Dismiss notification')!.click());
    try {
      await render();
      expect(container.textContent).toContain('Document preparation completed.');
      await dismiss();
      expect(JSON.parse(localStorage.getItem(storageKey)||'null')).toEqual([JSON.stringify(['one','COMPLETED',operation.updated_at])]);
      await act(async()=>root.unmount());
      localStorage.setItem(storageKey,JSON.stringify([
        ...Array.from({length:140},(_,index)=>JSON.stringify(['old-'+index,'COMPLETED','2026-11-01T00:00:00Z'])),
        null,17,{private_resume:'SENSITIVE_CV_TEXT'},'x'.repeat(1024),
      ]));
      root=createRoot(container);
      await render();
      expect(container.textContent).toContain('Document preparation completed.');
      await dismiss();
      const saved=JSON.parse(localStorage.getItem(storageKey)||'null') as string[];
      expect(saved).toHaveLength(100);
      expect(saved.every(key=>typeof key==='string'&&key.length<=512)).toBe(true);
      expect(JSON.stringify(saved)).not.toContain('SENSITIVE_CV_TEXT');
    } finally {await act(async()=>root.unmount());container.remove();localStorage.removeItem(storageKey);}
  });
});

describe('incomplete notification readback',()=>{
  let container:HTMLDivElement;
  let root:ReturnType<typeof createRoot>;
  let fetcher:ReturnType<typeof vi.fn>;
  const profileId='notification-readback-profile';
  const storageKey='mjobs-notification-dismissals:v1:'+profileId;
  beforeEach(()=>{
    localStorage.removeItem(storageKey);
    container=document.createElement('div');document.body.append(container);
    root=createRoot(container);
    fetcher=vi.fn();
    vi.stubGlobal('fetch',fetcher);
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);
  });
  afterEach(async()=>{
    await act(async()=>root.unmount());
    container.remove();localStorage.removeItem(storageKey);
    vi.unstubAllGlobals();
  });
  async function render(operations:NotificationOperation[],loadError:boolean,enabled=true) {
    await act(async()=>root.render(<OperationNotifications profileId={profileId}
      enabled={enabled} operations={operations} loadError={loadError}/>));
  }
  async function click(name:string) {
    await act(async()=>fireEvent.click(getByRole(container,'button',{name})));
  }

  it.each([
    {name:'empty readback',operations:[]},
    {name:'only a running operation',operations:[{...operation,state:'RUNNING'}]},
  ])('keeps $name inconclusive during a failed read and restores a successful empty state',async({operations})=>{
    await render(operations,true);
    expect(container.textContent).toContain('Activity updates are temporarily unavailable');
    expect(container.textContent).not.toContain('No new activity notifications.');
    expect(container.textContent).toContain('The notification list may be incomplete.');
    expect(queryByRole(container,'button',{name:'Dismiss notification'})).toBeNull();
    expect(localStorage.getItem(storageKey)).toBeNull();

    await render(operations,false);
    expect(container.textContent).toContain('No new activity notifications.');
    expect(container.textContent).not.toContain('temporarily unavailable');
    expect(container.textContent).not.toContain('may be incomplete');
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('retains known notifications, keeps dismissal during failure inconclusive, and shows later outcomes',async()=>{
    const saved={...operation,state:'FAILED',error:'PRIVATE_FAILURE_DETAIL',result:{text:'PRIVATE_RESULT_DETAIL'}};
    const before=JSON.stringify(saved);
    await render([saved],false);
    expect(container.textContent).toContain('Document preparation needs attention.');
    await render([saved],true);
    expect(container.textContent).toContain('Document preparation needs attention.');
    expect(container.textContent).toContain('Activity updates are temporarily unavailable');
    expect(container.textContent).not.toMatch(/PRIVATE_FAILURE_DETAIL|PRIVATE_RESULT_DETAIL/);
    await click('Dismiss notification');
    expect(container.textContent).not.toContain('Document preparation needs attention.');
    expect(container.textContent).not.toContain('No new activity notifications.');
    expect(container.textContent).toContain('The notification list may be incomplete.');
    expect(JSON.parse(localStorage.getItem(storageKey)||'null')).toEqual([
      JSON.stringify([saved.id,saved.state,saved.updated_at]),
    ]);

    await render([],false);
    expect(container.textContent).toContain('No new activity notifications.');
    expect(container.textContent).not.toContain('temporarily unavailable');
    await render([{...operation,updated_at:'2026-11-02T01:00:00Z'}],false);
    expect(container.textContent).toContain('Document preparation completed.');
    expect(getByRole(container,'button',{name:'Dismiss notification'})).toBeTruthy();
    expect(container.textContent).not.toContain('No new activity notifications.');
    expect(JSON.stringify(saved)).toBe(before);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('preserves opt-in and muting while reads fail without presenting an empty result on unmute',async()=>{
    await render([],true,false);
    expect(container.textContent).toBe('');
    await render([],true);
    await click('Mute for this visit');
    expect(container.textContent).not.toContain('temporarily unavailable');
    expect(container.textContent).not.toContain('No new activity notifications.');
    await render([],true);
    await click('Show notifications');
    expect(container.textContent).toContain('Activity updates are temporarily unavailable');
    expect(container.textContent).not.toContain('No new activity notifications.');
    expect(container.textContent).toContain('The notification list may be incomplete.');
    expect(localStorage.getItem(storageKey)).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });
});
