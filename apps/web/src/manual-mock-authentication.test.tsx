import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ManualMockAuthentication } from './manual-mock-authentication';

describe('manual mock authentication',()=>{
  const props={
    applicationId:'app-auth-required',
    company:'Acme Fixture',
    title:'Systems Administrator',
    onStartSignIn:vi.fn(),
    onRerun:vi.fn(),
  };

  it('shows user-started mock sign-in separately from an explicit rerun without collecting credentials or submitting',()=>{
    const html=renderToStaticMarkup(<ManualMockAuthentication {...props}/>);

    expect(html).toContain('Authentication is required');
    expect(html).toContain('MOCK fixture only');
    expect(html).toContain('Open MOCK sign-in');
    expect(html).toContain('Rerun MOCK application');
    expect(html).toContain('does not automatically rerun');
    expect(html).toContain('do not submit an application');
    expect(html).toContain('does not collect or display credentials');
    expect(html).not.toMatch(/<input|<form|<button[^>]*>Submit/i);
    expect(props.onStartSignIn).not.toHaveBeenCalled();
    expect(props.onRerun).not.toHaveBeenCalled();
  });

  it('routes each deliberate button action through its own callback with the application id',()=>{
    const element=ManualMockAuthentication(props);
    const sections=element.props.children as React.ReactElement[];
    const actionBar=sections[3] as React.ReactElement<{children:React.ReactElement<{onClick:()=>void}>[]}>;
    const buttons=actionBar.props.children;
    buttons[0].props.onClick();
    buttons[1].props.onClick();
    expect(props.onStartSignIn).toHaveBeenCalledExactlyOnceWith('app-auth-required');
    expect(props.onRerun).toHaveBeenCalledExactlyOnceWith('app-auth-required');
  });

  it('keeps application context and pending sign-in feedback visible',()=>{
    const html=renderToStaticMarkup(<ManualMockAuthentication {...props} signInPending/>);
    expect(html).toContain('Acme Fixture');
    expect(html).toContain('Systems Administrator');
    expect(html).toContain('Opening MOCK sign-in');
    expect(html).toContain('Rerun MOCK application');

    const element=ManualMockAuthentication({...props,signInPending:true});
    const sections=element.props.children as React.ReactElement[];
    const actionBar=sections[3] as React.ReactElement<{children:React.ReactElement<{disabled?:boolean}>[]}>;
    expect(actionBar.props.children[1].props.disabled).toBe(true);
  });

  it('offers explicit closed-browser recovery without rerunning or collecting credentials',()=>{
    const recover=vi.fn();
    const html=renderToStaticMarkup(<ManualMockAuthentication {...props}
      heldOperations={['held-operation']} onRecover={recover}/>);
    expect(html).toContain('Recover closed browser');
    expect(html).toContain('does not sign in or rerun');
    expect(html).toMatch(/disabled=""[^>]*>Open MOCK sign-in/);
    expect(html).toMatch(/disabled=""[^>]*>Rerun MOCK application/);
    expect(recover).not.toHaveBeenCalled();
  });
});
