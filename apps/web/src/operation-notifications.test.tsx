// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it} from 'vitest';
import {OperationNotifications, type NotificationOperation} from './operation-notifications';

const operation:NotificationOperation={id:'one',kind:'PREPARE',state:'COMPLETED',updated_at:'2026-11-02T00:00:00Z'};
describe('configured in-app notifications',()=>{
  it('does not display notifications without opt-in',()=>{
    expect(renderToStaticMarkup(<OperationNotifications enabled={false} operations={[operation]}/>)).toBe('');
  });
  it('shows only bounded terminal outcomes using fixed, private-data-free copy',()=>{
    const rows=Array.from({length:9},(_,index)=>({...operation,id:String(index),kind:'secret-token-abc'}));
    const html=renderToStaticMarkup(<OperationNotifications enabled operations={rows}/>);
    expect(html.match(/Dismiss notification/g)).toHaveLength(5);
    expect(html).toContain('Background task completed');
    expect(html).not.toContain('secret-token-abc');
    expect(renderToStaticMarkup(<OperationNotifications enabled operations={[{...operation,state:'RUNNING'}]}/>)).toContain('No new activity');
  });
  it('supports dismissal, a new outcome, and muting without writing or sending',async()=>{
    const container=document.createElement('div');document.body.append(container);
    const root=createRoot(container);
    try {
      await act(async()=>root.render(<OperationNotifications enabled operations={[operation]}/>));
      expect(container.textContent).toContain('Document preparation completed.');
      await act(async()=>Array.from(container.querySelectorAll('button')).find(b=>b.textContent==='Dismiss notification')!.click());
      expect(container.textContent).toContain('No new activity');
      await act(async()=>root.render(<OperationNotifications enabled operations={[{...operation,state:'FAILED'}]}/>));
      expect(container.textContent).toContain('needs attention');
      await act(async()=>Array.from(container.querySelectorAll('button')).find(b=>b.textContent==='Mute for this visit')!.click());
      expect(container.textContent).not.toContain('needs attention');
      await act(async()=>root.render(<OperationNotifications enabled={false} operations={[operation]}/>));
      expect(container.textContent).toBe('');
    } finally {await act(async()=>root.unmount());container.remove();}
  });
});
