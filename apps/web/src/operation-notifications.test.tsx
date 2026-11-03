// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it} from 'vitest';
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
