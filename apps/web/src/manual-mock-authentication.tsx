import React from 'react';

export type ManualMockAuthenticationProps = {
  applicationId:string;
  company:string;
  title:string;
  onStartSignIn:(applicationId:string)=>void;
  onRerun:(applicationId:string)=>void;
  signInPending?:boolean;
  rerunPending?:boolean;
  heldOperations?:string[];
  onRecover?:(applicationId:string,operationId:string)=>void;
  recoveryPending?:boolean;
};

export function ManualMockAuthentication({
  applicationId,
  company,
  title,
  onStartSignIn,
  onRerun,
  signInPending=false,
  rerunPending=false,
  heldOperations=[],
  onRecover,
  recoveryPending=false,
}:ManualMockAuthenticationProps) {
  return <section className="panel" aria-label="Manual MOCK authentication">
    <div className="toolbar"><div>
      <h2>Authentication is required</h2>
      <p>{company||'Company UNKNOWN'} — {title||'Job title UNKNOWN'}</p>
    </div><span className="tag">AUTH_REQUIRED</span></div>
    <p>This application is paused at an authentication boundary. You can open sign-in for the MOCK fixture only, then choose whether to rerun the application.</p>
    <p className="muted">Opening sign-in does not automatically rerun the application. Rerun is a separate action, and sign-in and rerun do not submit an application. Credentials are entered on the fixture sign-in page; this MJobs review does not collect or display credentials.</p>
    <div className="toolbar">
      <button type="button" disabled={signInPending||recoveryPending||heldOperations.length>0} aria-busy={signInPending} onClick={()=>onStartSignIn(applicationId)}>
        {signInPending?'Opening MOCK sign-in':'Open MOCK sign-in'}
      </button>
      <button type="button" disabled={signInPending||rerunPending||recoveryPending||heldOperations.length>0} aria-busy={rerunPending} onClick={()=>onRerun(applicationId)}>
        {rerunPending?'Rerunning MOCK application':'Rerun MOCK application'}
      </button>
    </div>
    {heldOperations.length>0&&<div className="safety">
      <p>A previous browser still holds this application. Close its window first. Recovery requires saved proof that the entire browser closed and the previous server stopped; restart MJobs if that server is still running. Recovery does not sign in or rerun the application.</p>
      {heldOperations.map(id=><button type="button" key={id}
        disabled={!onRecover||signInPending||rerunPending||recoveryPending}
        aria-busy={recoveryPending} onClick={()=>onRecover?.(applicationId,id)}>
        {recoveryPending?'Recovering closed browser':'Recover closed browser'}
      </button>)}
    </div>}
    <p className="muted">MOCK fixture only. External ATS access and external submission are not part of this sign-in flow.</p>
  </section>;
}
