// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, Profiler } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { getByRole, queryByRole } from '@testing-library/dom';
import { ApplicationFollowUpDrafts, FollowUpDraftsView, followUpDraftsPath, type FollowUpDraftCandidate } from './application-follow-up-drafts';

const apiMock=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:apiMock}));

const candidate:FollowUpDraftCandidate={
  application_id:'app-1',job_id:'job-1',job_title:'Systems Administrator',company:'Acme',days_quiet:8,
  reported_at:'2026-10-01T12:00:00Z',attribution:'EXTERNAL_USER_REPORTED',verified:false,
  email_subject:'Following up on my application',email_body:'I applied and would appreciate an update.',
  dm_body:'Hello, I recently applied and wanted to follow up.',
  character_limits:{subject:120,email_body:500,dm_body:280},
};

describe('follow-up drafts UI',()=>{
  it('builds the profile-scoped local API request with the selected quiet-days threshold',()=>{
    expect(followUpDraftsPath('profile / one',7)).toBe('/profiles/profile%20%2F%20one/application-follow-up-drafts?quiet_days=7');
  });

  it('renders the bounded quiet-days selector with its accessible default',()=>{
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={true} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('Follow-up drafts');
    expect(html).toContain('Quiet days before follow-up');
    expect(html).toContain('value="1">1 day</option>');
    expect(html).toContain('value="90">90 days</option>');
    expect(html).toContain('<option value="7" selected="">7 days</option>');
    expect(html).toContain('Loading follow-up drafts');
  });

  it('labels only reported and unverified evidence, shows bounded draft text, and offers copy only',()=>{
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[candidate]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('Acme');
    expect(html).toContain('Systems Administrator');
    expect(html).toContain('Reported by you · not verified');
    expect(html).toContain('Email subject');
    expect(html).toContain('Following up on my application');
    expect(html).toContain('30 / 120 characters');
    expect(html).toContain('I applied and would appreciate an update.');
    expect(html).toContain('DM wording');
    expect(html).toContain('Copy email subject');
    expect(html).toContain('Copy email body');
    expect(html).toContain('Copy DM wording');
    expect(html).not.toMatch(/>\s*Send\s*</i);
  });

  it('counts supplementary Unicode characters as one character against the API limit',()=>{
    const nearLimit={...candidate,email_subject:`${'a'.repeat(119)}😀`};
    const html=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[nearLimit]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(html).toContain('120 / 120 characters');
  });

  it('has explicit empty and API error states',()=>{
    const empty=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    const verified=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[{...candidate,verified:true}]} loading={false} error={null} copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    const error=renderToStaticMarkup(<FollowUpDraftsView quietDays={7} candidates={[]} loading={false} error="Local API unavailable" copied={null} onQuietDaysChange={vi.fn()} onCopy={vi.fn()}/>);
    expect(empty).toContain('No reported applications have been quiet for 7 days or more.');
    expect(verified).toContain('No reported applications have been quiet for 7 days or more.');
    expect(verified).not.toContain('Following up on my application');
    expect(error).toContain('Could not load follow-up drafts');
    expect(error).toContain('Local API unavailable');
  });
});

type DraftResponse={quiet_days:number;candidates:FollowUpDraftCandidate[]};
type Deferred<T>={promise:Promise<T>;resolve:(value:T)=>void;reject:(reason:unknown)=>void};

function deferred<T>():Deferred<T> {
  let resolve!:(value:T)=>void;
  let reject!:(reason:unknown)=>void;
  const promise=new Promise<T>((accept,decline)=>{resolve=accept;reject=decline;});
  return {promise,resolve,reject};
}

describe('connected follow-up draft context',()=>{
  let container:HTMLDivElement;
  let root:Root;
  let clipboardDescriptor:PropertyDescriptor|undefined;
  let requests:{path:string;result:Deferred<DraftResponse>}[];
  let copies:{text:string;result:Deferred<void>}[];
  let commits:{quietDays:string|undefined;text:string}[];

  beforeEach(()=>{
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);
    requests=[];copies=[];commits=[];
    apiMock.mockReset();
    apiMock.mockImplementation((path:string)=>{
      const result=deferred<DraftResponse>();
      requests.push({path,result});
      return result.promise;
    });
    clipboardDescriptor=Object.getOwnPropertyDescriptor(navigator,'clipboard');
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
      writeText:vi.fn((text:string)=>{
        const result=deferred<void>();
        copies.push({text,result});
        return result.promise;
      }),
    }});
    container=document.createElement('div');
    document.body.appendChild(container);
    root=createRoot(container);
  });

  afterEach(async()=>{
    await act(async()=>{root.unmount();});
    container.remove();
    if(clipboardDescriptor)Object.defineProperty(navigator,'clipboard',clipboardDescriptor);
    else Reflect.deleteProperty(navigator,'clipboard');
    vi.unstubAllGlobals();
  });

  async function render(profileId='profile-a') {
    await act(async()=>{
      root.render(<Profiler id="drafts" onRender={()=>{
        commits.push({quietDays:container.querySelector('select')?.value,text:container.textContent||''});
      }}><ApplicationFollowUpDrafts profileId={profileId}/></Profiler>);
    });
  }

  async function answer(requestIndex:number,rows:FollowUpDraftCandidate[]=[candidate]) {
    const request=requests[requestIndex];
    expect(request).toBeDefined();
    const quietDays=Number(new URL(request.path,'http://localhost').searchParams.get('quiet_days'));
    await act(async()=>{request.result.resolve({quiet_days:quietDays,candidates:rows});});
  }

  async function quietDays(days:number) {
    await act(async()=>{
      const select=getByRole(container,'combobox',{name:'Quiet days before follow-up'}) as HTMLSelectElement;
      select.value=String(days);
      select.dispatchEvent(new Event('change',{bubbles:true}));
    });
  }

  async function copy(name:string) {
    await act(async()=>{getByRole(container,'button',{name}).click();});
    return copies[copies.length-1];
  }

  function expectNoCommitTextSince(index:number,text:string) {
    const observed=commits.slice(index);
    expect(observed.length).toBeGreaterThan(0);
    for(const commit of observed)expect(commit.text).not.toContain(text);
  }

  it.each(['success','rejection'] as const)(
    'keeps the active clipboard error when an obsolete threshold copy finishes with %s',
    async outcome=>{
      await render();await answer(0);
      const oldCopy=await copy('Copy email body');
      expect(oldCopy.text).toBe(candidate.email_body);
      await quietDays(14);
      await answer(1,[{...candidate,days_quiet:20}]);
      const currentCopy=await copy('Copy DM wording');
      await act(async()=>{currentCopy.result.reject(new Error('Current clipboard denied'));});
      expect(getByRole(container,'alert').textContent).toContain('Clipboard access is unavailable');

      await act(async()=>{
        if(outcome==='success')oldCopy.result.resolve(undefined);
        else oldCopy.result.reject(new Error('Obsolete clipboard denied'));
      });

      expect(getByRole(container,'alert').textContent).toContain('Clipboard access is unavailable');
      expect(queryByRole(container,'button',{name:/^Copied /})).toBeNull();
    },
  );

  it('keeps feedback for the newest copy when earlier copies finish out of order',async()=>{
    await render();await answer(0);
    const oldEmail=await copy('Copy email body');
    const currentDM=await copy('Copy DM wording');
    expect(currentDM.text).toBe(candidate.dm_body);
    await act(async()=>{currentDM.result.resolve(undefined);});
    await act(async()=>{oldEmail.result.reject(new Error('Older clipboard denial'));});
    expect(getByRole(container,'button',{name:'Copied DM wording'})).toBeTruthy();
    expect(queryByRole(container,'alert')).toBeNull();

    const oldSubject=await copy('Copy email subject');
    const currentEmail=await copy('Copy email body');
    await act(async()=>{currentEmail.result.resolve(undefined);});
    await act(async()=>{oldSubject.result.resolve(undefined);});
    expect(getByRole(container,'button',{name:'Copied email body'})).toBeTruthy();
    expect(queryByRole(container,'button',{name:'Copied email subject'})).toBeNull();
  });

  it('does not reuse a pending clipboard result after returning to the same threshold',async()=>{
    await render();await answer(0);
    const oldCopy=await copy('Copy email body');
    await quietDays(14);await answer(1,[{...candidate,days_quiet:20}]);
    await quietDays(7);await answer(2);
    const currentCopy=await copy('Copy email body');
    await act(async()=>{currentCopy.result.reject(new Error('Current clipboard denied'));});
    await act(async()=>{oldCopy.result.resolve(undefined);});

    expect(getByRole(container,'alert').textContent).toContain('Clipboard access is unavailable');
    expect(queryByRole(container,'button',{name:'Copied email body'})).toBeNull();
  });

  it('changes the interval without committing old drafts or errors and keeps selector focus',async()=>{
    await render();await answer(0,[{...candidate,email_body:'Seven-day private draft'}]);
    const select=getByRole(container,'combobox',{name:'Quiet days before follow-up'});
    select.focus();
    const beforeChange=commits.length;
    await quietDays(14);

    expectNoCommitTextSince(beforeChange,'Seven-day private draft');
    expect(commits.slice(beforeChange).every(commit=>commit.quietDays==='14')).toBe(true);
    expect(getByRole(container,'combobox',{name:'Quiet days before follow-up'})).toBe(select);
    expect(document.activeElement).toBe(select);
    expect(queryByRole(container,'button',{name:'Copy email body'})).toBeNull();
    expect(requests[1].path).toBe('/profiles/profile-a/application-follow-up-drafts?quiet_days=14');

    await act(async()=>{requests[1].result.reject(new Error('Fourteen-day lookup failed'));});
    expect(getByRole(container,'alert').textContent).toContain('Fourteen-day lookup failed');
    const beforeRetry=commits.length;
    await quietDays(21);
    expectNoCommitTextSince(beforeRetry,'Fourteen-day lookup failed');
    expect(document.activeElement).toBe(select);
    expect(getByRole(container,'status').textContent).toContain('Loading follow-up drafts');
  });

  it('does not commit drafts or lookup errors from the previous profile',async()=>{
    await render('profile-a');await answer(0,[{...candidate,email_body:'Private profile A draft'}]);
    const beforeProfileChange=commits.length;
    await render('profile-b');
    expectNoCommitTextSince(beforeProfileChange,'Private profile A draft');
    expect(requests[1].path).toBe('/profiles/profile-b/application-follow-up-drafts?quiet_days=7');
    expect(queryByRole(container,'button',{name:'Copy email body'})).toBeNull();

    await act(async()=>{requests[1].result.reject(new Error('Profile B lookup failed'));});
    expect(getByRole(container,'alert').textContent).toContain('Profile B lookup failed');
    const beforeNextProfile=commits.length;
    await render('profile-c');
    expectNoCommitTextSince(beforeNextProfile,'Profile B lookup failed');
    await answer(2,[{...candidate,email_body:'Current profile C draft'}]);
    expect(container.textContent).toContain('Current profile C draft');
  });

  it('ignores superseded lookup responses and errors even after returning to the same profile',async()=>{
    await render('profile-a');
    await render('profile-b');
    await render('profile-a');
    await answer(2,[{...candidate,email_body:'Fresh profile A draft'}]);
    await answer(0,[{...candidate,email_body:'Obsolete profile A draft'}]);
    await act(async()=>{requests[1].result.reject(new Error('Obsolete profile B failure'));});

    expect(container.textContent).toContain('Fresh profile A draft');
    expect(container.textContent).not.toContain('Obsolete profile A draft');
    expect(queryByRole(container,'alert')).toBeNull();
    const copied=await copy('Copy email body');
    expect(copied.text).toBe('Fresh profile A draft');
  });

  it('hides drafts and makes no lookup when the profile is cleared, including late responses',async()=>{
    await render();await answer(0,[{...candidate,email_body:'Previously selected private draft'}]);
    const beforeClear=commits.length;
    await render('');
    expectNoCommitTextSince(beforeClear,'Previously selected private draft');
    expect(requests).toHaveLength(1);
    expect(queryByRole(container,'button',{name:/^Copy /})).toBeNull();

    await render('profile-b');
    expect(requests).toHaveLength(2);
    await render('');
    await answer(1,[{...candidate,email_body:'Late profile B draft'}]);
    expect(requests).toHaveLength(2);
    expect(container.textContent).not.toContain('Late profile B draft');
    expect(queryByRole(container,'button',{name:/^Copy /})).toBeNull();
    expect(queryByRole(container,'alert')).toBeNull();
  });
});
