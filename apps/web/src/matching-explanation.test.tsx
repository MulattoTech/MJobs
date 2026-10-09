import React, { type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  MatchingExplanationPanel,
  type MatchingExplanationPanelProps,
  type MatchExplanationResponse,
} from './matching-explanation';

const response:MatchExplanationResponse = {
  profile_id:'profile-1',search_profile_id:'search-cloud',job_id:'job-1',score:82.5,
  resolution:'CURRENT',
  profile_resolution:{
    requested_role_family:'Cloud Operations',selected_role_family:'Cloud Operations',
    profile_id:'scoring-1',profile_version:'version-4',selection:'ROLE_FAMILY',
  },
  scoring_profile:{id:'scoring-1',version:'version-4',role_family:'Cloud Operations'},
  scoring_contributions:[
    {dimension:'skills',weight:60,raw_score:75,weighted_contribution:45,
      explanation:'Approved Kubernetes and Linux skills align with this posting.',
      provenance:{source:'approved_profile_skills',evidence_ref:'fact:skill-17'}},
    {dimension:'ai_intensity',weight:20,raw_score:null,weighted_contribution:null,
      explanation:'AI intensity is UNKNOWN because source metadata is unavailable.',
      provenance:{source:'UNKNOWN'}},
  ],
};

function props(overrides:Partial<MatchingExplanationPanelProps> = {}):MatchingExplanationPanelProps {
  return {
    profileId:'profile-1',searchProfileId:'search-cloud',jobId:'job-1',
    state:'ready',explanation:response,error:null,onRetry:undefined,...overrides,
  };
}

function html(overrides:Partial<MatchingExplanationPanelProps> = {}):string {
  return renderToStaticMarkup(<MatchingExplanationPanel {...props(overrides)}/>);
}

function button(tree:ReactElement<any>,name:string):ReactElement<any>|null {
  function find(node:unknown):ReactElement<any>|null {
    if(Array.isArray(node)) {
      for(const child of node) {
        const found=find(child);
        if(found) return found;
      }
      return null;
    }
    if(!React.isValidElement(node)) return null;
    const element=node as ReactElement<any>;
    if(element.type==='button'&&element.props.children===name) return element;
    return find(element.props.children);
  }
  return find(tree);
}

describe('matching explanation panel',()=>{
  it('shows score, selected role profile attribution, contributions, and provenance as advisory',()=>{
    const markup=html();
    expect(markup).toContain('82.5');
    expect(markup).toContain('Cloud Operations');
    expect(markup).toContain('scoring-1');
    expect(markup).toContain('version-4');
    expect(markup).toContain('skills');
    expect(markup).toContain('75');
    expect(markup).toContain('45');
    expect(markup).toContain('Approved Kubernetes and Linux skills align');
    expect(markup).toContain('approved_profile_skills');
    expect(markup).toContain('fact:skill-17');
    expect(markup).toContain('Advisory score');
    expect(markup).toContain('does not change hard eligibility');
    expect(markup).toContain('aria-label="Scoring signal contributions"');
    expect(markup).not.toContain('<form');
    expect(markup).not.toContain('Submit application');
    expect(markup).not.toContain('Generate');
  });

  it('renders absent raw score and contribution as explicit UNKNOWN with provenance',()=>{
    const markup=html();
    expect(markup).toContain('AI intensity');
    expect(markup).toContain('<th scope="row">AI intensity</th>');
    expect(markup).toContain('<th scope="col">Raw score</th>');
    expect(markup).toContain('<th scope="col">Weighted contribution</th>');
    expect(markup).toContain('<td>UNKNOWN</td><td>UNKNOWN</td>');
    expect(markup).toContain('AI intensity is UNKNOWN');
    expect(markup).toContain('UNKNOWN');
  });

  it('keeps loading and unavailable states free of prior score details',()=>{
    const loading=html({state:'loading'});
    expect(loading).toContain('role="status"');
    expect(loading).toContain('Loading saved match explanation');
    expect(loading).not.toContain('82.5');
    const empty=html({state:'empty',explanation:null});
    expect(empty).toContain('No saved score breakdown is available');
    expect(empty).not.toContain('scoring-1');
  });

  it('shows a safe error and invokes retry only after explicit user action',()=>{
    const onRetry=vi.fn();
    const markup=html({state:'error',explanation:null,error:'Saved explanation could not be loaded.',onRetry});
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('Saved explanation could not be loaded.');
    expect(markup).not.toContain('82.5');
    const tree=MatchingExplanationPanel(props({state:'error',explanation:null,error:'Unavailable',onRetry}));
    const retry=button(tree,'Retry explanation');
    expect(retry).not.toBeNull();
    retry?.props.onClick();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['profileId','profile-2'],
    ['searchProfileId','search-other'],
    ['jobId','job-2'],
  ] as const)('clears a saved explanation when selected %s changes',(_field,value)=>{
    const markup=html({[_field]:value});
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('does not match the selected profile, saved search, and job');
    expect(markup).not.toContain('82.5');
    expect(markup).not.toContain('Approved Kubernetes');
    expect(markup).not.toContain('fact:skill-17');
  });

  it('fails closed on invalid contribution data or inconsistent profile versions',()=>{
    const malformed={...response,scoring_contributions:[{
      ...response.scoring_contributions[0],weight:101,
    }]} as unknown as MatchExplanationResponse;
    const markup=html({explanation:malformed});
    expect(markup).toContain('Saved score explanation is unavailable');
    expect(markup).not.toContain('82.5');
    const staleProfile={...response,profile_resolution:{
      ...response.profile_resolution,profile_version:'old-version',
    }};
    const staleMarkup=html({explanation:staleProfile});
    expect(staleMarkup).toContain('Saved score explanation is unavailable');
    expect(staleMarkup).not.toContain('Approved Kubernetes');
    const inconsistentUnknown={...response,scoring_contributions:[{
      ...response.scoring_contributions[1],weighted_contribution:1,
    }]} as unknown as MatchExplanationResponse;
    const inconsistentMarkup=html({explanation:inconsistentUnknown});
    expect(inconsistentMarkup).toContain('Saved score explanation is unavailable');
    expect(inconsistentMarkup).not.toContain('82.5');
  });

  it('labels a legacy score without inventing a selected profile',()=>{
    const legacy:MatchExplanationResponse={
      ...response,resolution:'LEGACY_DEFAULT',scoring_profile:null,
      profile_resolution:{
        requested_role_family:null,selected_role_family:null,
        profile_id:null,profile_version:null,selection:'LEGACY_DEFAULT',
      },
    };
    const markup=html({explanation:legacy});
    expect(markup).toContain('Legacy default scoring');
    expect(markup).not.toContain('scoring-1');
    expect(markup).toContain('82.5');
  });
});
