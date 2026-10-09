import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationMultipageReview,
  ApplicationMultipageReviewView,
  canContinueApplicationPage,
  moveApplicationMultipageReview,
  saveApplicationMultipageProgress,
  type ApplicationMultipagePage,
  type ApplicationMultipageState,
} from './application-multipage-review';

const pages:ApplicationMultipagePage[]=[
  {id:'contact',title:'Contact information',fields:[
    {id:'email',label:'Email',required:true},
    {id:'phone',label:'Phone',required:false},
  ]},
  {id:'experience',title:'Experience',fields:[
    {id:'summary',label:'Experience summary',required:true},
  ]},
];

const state:ApplicationMultipageState={
  profile_id:'profile-a',application_id:'application-a',current_page_id:'contact',
  page_order:['contact','experience'],status:'IN_PROGRESS',progress:{contact:{email:'person@example.test',phone:''}},
  version:2,updated_at:'2026-10-09T12:00:00Z',manual_intervention_required:true,
};

describe('multi-page application review',()=>{
  it('requires nonblank values for every required field on the current page before continuing',()=>{
    expect(canContinueApplicationPage(pages[0],{email:'  ',phone:'555-0100'})).toEqual(['Email']);
    expect(canContinueApplicationPage(pages[0],{email:'person@example.test'})).toEqual([]);
  });

  it('saves a valid page before advancing and enters a manual review stop without a submit action',async()=>{
    const client={load:vi.fn(),transition:vi.fn()
      .mockResolvedValueOnce({...state,version:3})
      .mockResolvedValueOnce({...state,current_page_id:'experience',version:4})};
    await moveApplicationMultipageReview(client,'profile-a','application-a',state,pages,state.progress,'NEXT');
    expect(client.transition).toHaveBeenNthCalledWith(1,'profile-a','application-a',expect.objectContaining({
      action:'SAVE_PROGRESS',expected_version:2,page_id:'contact',page_order:['contact','experience'],progress:state.progress,
    }));
    expect(client.transition).toHaveBeenNthCalledWith(2,'profile-a','application-a',expect.objectContaining({
      action:'NEXT',expected_version:3,page_id:'contact',next_page_id:'experience',
    }));
    const enteringReview={...state,current_page_id:'experience',version:4};
    client.transition.mockResolvedValueOnce({...enteringReview,progress:{...state.progress,experience:{summary:'Relevant experience'}},version:5});
    client.transition.mockResolvedValueOnce({...enteringReview,current_page_id:'review',status:'REVIEW',
      progress:{...state.progress,experience:{summary:'Relevant experience'}},version:6});
    await moveApplicationMultipageReview(client,'profile-a','application-a',enteringReview,pages,
      {...state.progress,experience:{summary:'Relevant experience'}},'NEXT');
    expect(client.transition).toHaveBeenLastCalledWith('profile-a','application-a',expect.objectContaining({
      action:'REVIEW',expected_version:5,page_id:'experience',
    }));
  });

  it('does not write when a required value is missing',async()=>{
    const client={load:vi.fn(),transition:vi.fn()};
    await expect(moveApplicationMultipageReview(client,'profile-a','application-a',state,pages,{contact:{email:' '}},'NEXT'))
      .rejects.toThrow('Complete required fields before continuing: Email.');
    expect(client.transition).not.toHaveBeenCalled();
  });

  it('initializes empty server progress with the ordered pages and exact nested field values',async()=>{
    const empty:ApplicationMultipageState={...state,current_page_id:null,page_order:[],progress:{},version:0};
    const saved={...empty,current_page_id:'contact',page_order:['contact','experience'],
      progress:{contact:{email:'person@example.test'}},version:1};
    const client={load:vi.fn(),transition:vi.fn().mockResolvedValue(saved)};
    await saveApplicationMultipageProgress(client,'profile-a','application-a',empty,pages,{contact:{email:'person@example.test'}});
    expect(client.transition).toHaveBeenCalledWith('profile-a','application-a',expect.objectContaining({
      action:'SAVE_PROGRESS',expected_version:0,page_id:'contact',page_order:['contact','experience'],
      progress:{contact:{email:'person@example.test'}},
    }));
  });

  it('returns from the manual review checkpoint to the last form page',async()=>{
    const reviewState={...state,current_page_id:'review',status:'REVIEW' as const};
    const client={load:vi.fn(),transition:vi.fn().mockResolvedValue({...reviewState,current_page_id:'experience',status:'IN_PROGRESS'})};
    await moveApplicationMultipageReview(client,'profile-a','application-a',reviewState,pages,state.progress,'PREVIOUS');
    expect(client.transition).toHaveBeenCalledWith('profile-a','application-a',expect.objectContaining({
      action:'PREVIOUS',expected_version:2,page_id:'review',previous_page_id:'experience',
    }));
  });

  it('refuses to save state loaded for another profile or application',async()=>{
    const client={load:vi.fn(),transition:vi.fn()};
    await expect(saveApplicationMultipageProgress(client,'profile-a','application-a',
      {...state,application_id:'application-other'},pages,state.progress)).rejects.toThrow('no longer matches');
    expect(client.transition).not.toHaveBeenCalled();
  });

  it('hides the previous application while the newly selected scope is still loading',()=>{
    const html=renderToStaticMarkup(<ApplicationMultipageReview profileId="profile-b" applicationId="application-b"
      pages={pages} initialState={state} client={{load:vi.fn(),transition:vi.fn()}}/>);
    expect(html).toContain('Loading saved application progress');
    expect(html).not.toContain('person@example.test');
    expect(html).not.toContain('Continue');
    expect(html).not.toContain('Manual intervention required');
  });

  it('prevents a stale previous-scope page from advancing or entering review',async()=>{
    const client={load:vi.fn(),transition:vi.fn()};
    const staleFinalPage={...state,current_page_id:'experience',progress:{experience:{summary:'Persisted prior application'}}};
    await expect(moveApplicationMultipageReview(client,'profile-b','application-b',staleFinalPage,pages,
      staleFinalPage.progress,'NEXT')).rejects.toThrow('no longer matches');
    expect(client.transition).not.toHaveBeenCalled();
  });

  it('shows progress, previous/continue/save controls, and required-field guidance',()=>{
    const html=renderToStaticMarkup(<ApplicationMultipageReviewView pages={pages} state={state} draft={state.progress.contact}
      pending={false} error={null} onFieldChange={vi.fn()} onPrevious={vi.fn()} onContinue={vi.fn()} onSaveProgress={vi.fn()} onReload={vi.fn()}/>);
    expect(html).toContain('Page 1 of 3');
    expect(html).toContain('Contact information');
    expect(html).toContain('aria-label="Application progress"');
    expect(html).toContain('required');
    expect(html).toContain('Previous');
    expect(html).toContain('Continue');
    expect(html).toContain('Save progress');
    expect(html).not.toMatch(/<button[^>]*>[^<]*submit[^<]*<\/button>/i);
  });

  it('makes the final review a clear manual-intervention stop without submit controls',()=>{
    const html=renderToStaticMarkup(<ApplicationMultipageReviewView pages={pages}
      state={{...state,current_page_id:'review',status:'REVIEW'}} draft={{}}
      pending={false} error={null} onFieldChange={vi.fn()} onPrevious={vi.fn()} onContinue={vi.fn()} onSaveProgress={vi.fn()} onReload={vi.fn()}/>);
    expect(html).toContain('Manual intervention required');
    expect(html).toContain('Review the employer form yourself and submit it on the employer’s website.');
    expect(html).not.toMatch(/<button[^>]*>[^<]*submit[^<]*<\/button>/i);
    expect(html).not.toMatch(/\bSubmit application\b/i);
  });

  it('surfaces a durable progress-save error while preserving the current draft',()=>{
    const html=renderToStaticMarkup(<ApplicationMultipageReviewView pages={pages} state={state}
      draft={{email:'edited@example.test'}} pending={false} error="Saved progress could not be read back."
      onFieldChange={vi.fn()} onPrevious={vi.fn()} onContinue={vi.fn()} onSaveProgress={vi.fn()} onReload={vi.fn()}/>);
    expect(html).toContain('Saved progress could not be read back.');
    expect(html).toContain('value="edited@example.test"');
    expect(html).toContain('Reload saved progress');
  });
});
