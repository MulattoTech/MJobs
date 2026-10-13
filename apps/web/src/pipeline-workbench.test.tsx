import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  parsePipelineWorkbench,
  PipelineWorkbenchView,
  queuePipelinePreparation,
  requestIdForPipelinePreparation,
  pipelinePreparationNotice,
  queuePipelineScoring,
  pipelineScoringNotice,
  type PipelineWorkbenchResponse,
  type PipelineWorkbenchItem,
} from './pipeline-workbench';

const item = (overrides:Partial<PipelineWorkbenchItem> = {}):PipelineWorkbenchItem => ({
  job_id:'private-job-a',title:'Systems Administrator',company:'Northwind Systems',score:91,
  application_id:null,stage:'READY_TO_PREPARE',blockers:[],can_prepare:true,
  fingerprint:'private-readiness-fingerprint',route_id:'private-route-a',
  next_action:'PREPARE',operation_id:null,...overrides,
});
const workbench:PipelineWorkbenchResponse = {
  profile_id:'profile-a',profile_version:'private-profile-version',
  counts:{total:3,by_stage:{DISCOVERED:1,NEEDS_SCORING:0,NEEDS_REVIEW:1,READY_TO_PREPARE:1,PREPARING:0,PREPARED:0,TRACKING:0}},
  items:[
    item(),
    item({job_id:'private-job-b',title:'Cloud Administrator',company:'Acme',stage:'NEEDS_REVIEW',score:null,
      blockers:[{code:'ROUTE_REVIEW_REQUIRED',message:'Choose the application route that matches this posting.'}],can_prepare:false,
      fingerprint:null,route_id:null,next_action:'REVIEW_ROUTE'}),
    item({job_id:'private-job-c',title:'Network Engineer',company:'Example Co',stage:'DISCOVERED',score:null,
      blockers:[{code:'SCORE_MISSING',message:'This job needs a current profile score.'}],
      can_prepare:false,fingerprint:null,route_id:null,next_action:'REVIEW_MATCH'}),
  ],
};

type TestElement = React.ReactElement<{children?:React.ReactNode; [key:string]:any}>;
function findElement(node:React.ReactNode,predicate:(element:TestElement)=>boolean):TestElement|null {
  if(Array.isArray(node))for(const child of node){const found=findElement(child,predicate);if(found)return found;}
  if(!React.isValidElement(node))return null;
  const element=node as TestElement;
  if(predicate(element))return element;
  return findElement(element.props.children,predicate);
}

describe('PipelineWorkbenchView',()=>{
  it('labels scoring as profile-wide and blocks preparation while scoring is active',()=>{
    const response={...workbench,scoring_operation:{operation_id:'private-score-op',state:'RUNNING' as const}};
    const tree=PipelineWorkbenchView({profileId:'profile-a',workbench:response,loading:false,error:false,
      onRetry:vi.fn(),onPrepare:vi.fn(),onRefreshScores:vi.fn()});
    const refresh=findElement(tree,element=>element.type==='button'&&element.props.children==='Refresh all scores for this profile');
    const prepare=findElement(tree,element=>element.type==='button'&&element.props.children==='Prepare application documents');
    expect(refresh?.props.disabled).toBe(true);
    expect(prepare?.props.disabled).toBe(true);
    const html=renderToStaticMarkup(tree);
    expect(html).toContain('No new source collection or employer submission');
    expect(html).not.toContain('private-score-op');
  });

  it('queues only the requested profile and stable request identity, rejecting malformed acknowledgements',async()=>{
    const send=vi.fn(async()=>({operation_id:'operation-1',state:'QUEUED'}));
    await expect(queuePipelineScoring('profile-a','request-1',send)).resolves.toEqual({operation_id:'operation-1',state:'QUEUED'});
    expect(send).toHaveBeenCalledExactlyOnceWith({profile_id:'profile-a',request_id:'request-1'});
    for(const response of [{operation_id:'',state:'QUEUED'},{operation_id:'x',state:'SUBMITTED'},null]) {
      await expect(queuePipelineScoring('profile-a','request-1',async()=>response)).rejects.toThrow('PIPELINE_SCORING_ACK_INVALID');
    }
  });

  it('validates persisted score states and uses fixed terminal copy',()=>{
    for(const state of ['QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','BLOCKED'] as const){
      expect(parsePipelineWorkbench({...workbench,scoring_operation:{operation_id:'op',state}},'profile-a').scoring_operation?.state).toBe(state);
      expect(pipelineScoringNotice(state)).not.toContain('submitted');
    }
    expect(()=>parsePipelineWorkbench({...workbench,scoring_operation:{operation_id:'op',state:'UNKNOWN'}},'profile-a')).toThrow();
    expect(()=>parsePipelineWorkbench({...workbench,scoring_operation:{operation_id:5,state:'RUNNING'}},'profile-a')).toThrow();
  });
  it('groups readiness stages and shows safe blockers and unknown scores distinctly',()=>{
    const html=renderToStaticMarkup(<PipelineWorkbenchView profileId="profile-a" workbench={workbench}
      loading={false} error={false} onRetry={vi.fn()} onPrepare={vi.fn()} onOpenApplication={vi.fn()}/>);
    for(const text of ['3 jobs in this profile','Newly discovered','Needs review','Ready to prepare',
      'Systems Administrator','Northwind Systems','91','Unknown score — not treated as zero',
      'Choose the application route that matches this posting.','This job needs a current profile score.',
      'Queue acceptance is not employer submission']) expect(html).toContain(text);
    for(const privateValue of ['private-job-a','private-job-b','private-profile-version','private-readiness-fingerprint','private-route-a']) {
      expect(html).not.toContain(privateValue);
    }
  });

  it('opens an existing application only through the supplied callback',()=>{
    const onOpenApplication=vi.fn();
    const ready=workbench.items[0];
    const response={...workbench,items:[{...ready,application_id:'private-app-1'}]};
    const tree=PipelineWorkbenchView({profileId:'profile-a',workbench:response,loading:false,error:false,
      onRetry:vi.fn(),onPrepare:vi.fn(),onOpenApplication});
    const openButton=findElement(tree,element=>element.type==='button'&&element.props.children==='Open existing application');
    expect(openButton).not.toBeNull();
    openButton?.props.onClick();
    expect(onOpenApplication).toHaveBeenCalledExactlyOnceWith('private-app-1');
  });

  it('shows safe load, scope, empty, and queued states without claiming submission',()=>{
    const render=(props:Partial<React.ComponentProps<typeof PipelineWorkbenchView>>) => renderToStaticMarkup(
      <PipelineWorkbenchView profileId="profile-a" workbench={null} loading={false} error={false}
        onRetry={vi.fn()} onPrepare={vi.fn()} onOpenApplication={vi.fn()} {...props}/>);
    expect(render({loading:true})).toContain('Loading pipeline readiness');
    expect(render({error:true})).toContain('role="alert"');
    expect(render({error:true})).not.toContain('secret backend error');
    expect(render({profileId:null})).toContain('Select a profile to view its pipeline');
    expect(render({workbench:{...workbench,profile_id:'profile-b'}})).toContain('does not match the selected profile');
    expect(render({workbench:{...workbench,items:[],counts:{total:0,by_stage:{}}}})).toContain('No jobs are available in this profile’s pipeline yet');
    expect(render({workbench,notice:'Preparation queued. It has not been submitted.'})).toContain('Preparation queued. It has not been submitted.');
  });

  it('requires profile version, readiness fingerprint, and an explicit prepare-ready item',async()=>{
    const client={prepare:vi.fn(async()=>({request_id:'request-1',job_id:'private-job-a',operation_id:'private-operation-1',state:'QUEUED',next_action:'REFRESH_WORKBENCH'}))};
    const acknowledgement=await queuePipelinePreparation(client,'profile-a',workbench.profile_version,item(), 'request-1');
    expect(acknowledgement.state).toBe('QUEUED');
    expect(client.prepare).toHaveBeenCalledExactlyOnceWith('profile-a','private-job-a',{
      request_id:'request-1',expected_profile_version:'private-profile-version',fingerprint:'private-readiness-fingerprint',
    });
    await expect(queuePipelinePreparation(client,'profile-a',workbench.profile_version,
      item({can_prepare:false}), 'request-2')).rejects.toThrow('PIPELINE_ITEM_NOT_READY');
    await expect(queuePipelinePreparation(client,'profile-a',workbench.profile_version,
      item({stage:'NEEDS_REVIEW'}), 'request-stage-mismatch')).rejects.toThrow('PIPELINE_ITEM_NOT_READY');
    const invalidClient={prepare:vi.fn(async()=>({state:'SUBMITTED'}))};
    await expect(queuePipelinePreparation(invalidClient,'profile-a',workbench.profile_version,
      item(), 'request-3')).rejects.toThrow('PIPELINE_QUEUE_ACK_INVALID');
  });

  it('keeps an ambiguous retry on its request id and accepts a durable terminal replay',async()=>{
    const requestIds=new Map<string,string>();
    const createId=vi.fn(()=> 'request-retry-1');
    const first=requestIdForPipelinePreparation(requestIds,'profile-a','private-job-a',createId);
    const ambiguousClient={prepare:vi.fn(async()=>{throw new Error('network disconnected after request dispatch');})};
    await expect(queuePipelinePreparation(ambiguousClient,'profile-a',workbench.profile_version,item(),first)).rejects.toThrow();
    const retry=requestIdForPipelinePreparation(requestIds,'profile-a','private-job-a',createId);
    expect(retry).toBe(first);
    expect(createId).toHaveBeenCalledTimes(1);
    const client={prepare:vi.fn(async()=>({request_id:first,job_id:'private-job-a',operation_id:'private-operation-1',state:'COMPLETED',next_action:'REFRESH_WORKBENCH'}))};
    const replay=await queuePipelinePreparation(client,'profile-a',workbench.profile_version,item(),first);
    expect(replay.state).toBe('COMPLETED');
    expect(replay.operation_id).toBe('private-operation-1');
    expect(pipelinePreparationNotice(replay.state)).toBe('Preparation completed. No employer submission was made.');
    expect(pipelinePreparationNotice('QUEUED')).toBe('Preparation queued. It has not been submitted.');
    const malformed={prepare:vi.fn(async()=>({request_id:first,job_id:'private-job-a',state:'COMPLETED',next_action:'REFRESH_WORKBENCH'}))};
    await expect(queuePipelinePreparation(malformed,'profile-a',workbench.profile_version,item(),first)).rejects.toThrow('PIPELINE_QUEUE_ACK_INVALID');
  });

  it('validates the selected profile and only permits known stages and bounded fields',()=>{
    expect(parsePipelineWorkbench(workbench,'profile-a').items).toHaveLength(3);
    expect(()=>parsePipelineWorkbench({...workbench,profile_id:'profile-b'},'profile-a')).toThrow('PIPELINE_SCOPE_MISMATCH');
    expect(()=>parsePipelineWorkbench({...workbench,items:[item({stage:'SUBMITTED' as never})]},'profile-a')).toThrow('PIPELINE_RESPONSE_INVALID');
    expect(()=>parsePipelineWorkbench({...workbench,items:[item({blockers:[{code:'UNKNOWN',message:'private raw error'}]})]},'profile-a')).toThrow('PIPELINE_RESPONSE_INVALID');
  });
});
