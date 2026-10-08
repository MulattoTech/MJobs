import React, { useCallback, useEffect, useRef, useState } from 'react';
import { CareerStoryBank, type ApprovedCareerFact, type CareerStory, type CareerStoryRevision, type CareerStoryRevisionInput, type CareerStoryRevisionResult } from './career-story-bank';
import { groupStoryRows, toCreatePayload, type CareerStoryApiRow, type CareerStoryRevisionView } from './career-story-bank-adapter';

type ProfileFact = {id:string;fact_id?:string;kind:string;text:string;source:string;verified:boolean;user_approved:boolean};
type ApiCall = <T>(path:string,init?:RequestInit)=>Promise<T>;

export function groupCareerStories(rows:readonly CareerStoryApiRow[]):CareerStory[] {
  return groupStoryRows(rows).map(story=>({
    storyId:story.storyId,
    revisions:story.revisions.map(revision=>({
      revisionId:revision.revisionId,
      revision:revision.revision,
      createdAt:revision.createdAt,
      title:revision.title,
      situation:revision.situation,
      task:revision.task,
      action:revision.action,
      result:revision.result,
      factIds:revision.factIds,
      allowedUses:revision.allowedUses,
      factFingerprints:revision.factFingerprints.map(value=>({factId:value.fact_id,fingerprint:value.fingerprint})),
      provenance:revision.provenance,
      reviewStatus:revision.reviewStatus as CareerStoryRevision['reviewStatus'],
      reviewedAt:revision.reviewedAt,
      unstructuredText:revision.unstructuredText??null,
      staleFactIds:revision.staleFactIds,
      isLatest:revision.isLatest,
    })),
  }));
}

export function toCareerStoryBankRows(facts:readonly ProfileFact[]):ApprovedCareerFact[] {
  return facts.map(fact=>({
    factId:fact.fact_id||fact.id,
    kind:fact.kind,
    text:fact.text,
    source:fact.source,
    verified:fact.verified,
    userApproved:fact.user_approved,
  }));
}

function mapRevision(revision:CareerStoryRevisionView):CareerStoryRevision {
  return {
    revisionId:revision.revisionId,
    revision:revision.revision,
    createdAt:revision.createdAt,
    title:revision.title,
    situation:revision.situation,
    task:revision.task,
    action:revision.action,
    result:revision.result,
    factIds:revision.factIds,
    allowedUses:revision.allowedUses,
    factFingerprints:revision.factFingerprints.map(value=>({factId:value.fact_id,fingerprint:value.fingerprint})),
    provenance:revision.provenance,
    reviewStatus:revision.reviewStatus as CareerStoryRevision['reviewStatus'],
    reviewedAt:revision.reviewedAt,
    unstructuredText:revision.unstructuredText??null,
    staleFactIds:revision.staleFactIds,
    isLatest:revision.isLatest,
  };
}

export function CareerStoryBankConnected({profileId,facts,api}: {
  profileId:string;
  facts:readonly ProfileFact[];
  api:ApiCall;
}) {
  const [stories,setStories]=useState<CareerStory[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const sequence=useRef(0);
  const profilePath=`/profiles/${encodeURIComponent(profileId)}/career-stories`;
  const load=useCallback(async()=>{
    const request=++sequence.current;
    setLoading(true);setError('');
    try {
      const response=await api<{profile_id:string;stories:CareerStoryApiRow[]}>(profilePath);
      if(request!==sequence.current)return [];
      if(response.profile_id!==profileId)throw new Error('Career story response belongs to a different profile.');
      const mapped=groupCareerStories(response.stories||[]);
      setStories(mapped);
      return mapped;
    } catch(failure) {
      if(request===sequence.current)setError(failure instanceof Error?failure.message:String(failure));
      throw failure;
    } finally {
      if(request===sequence.current)setLoading(false);
    }
  },[api,profileId,profilePath]);

  useEffect(()=>{
    void load().catch(()=>undefined);
    return()=>{sequence.current+=1;};
  },[load]);

  const createRevision=useCallback(async(input:CareerStoryRevisionInput):Promise<CareerStoryRevisionResult>=>{
    const prior=input.storyId?stories.find(story=>story.storyId===input.storyId):undefined;
    const latest=prior?[...prior.revisions].sort((a,b)=>b.revision-a.revision)[0]:undefined;
    if(input.storyId&&!latest)throw new Error('Reload this career story before saving another revision.');
    const payload=toCreatePayload(input);
    const path=input.storyId&&latest
      ?`${profilePath}/${encodeURIComponent(latest.revisionId)}/versions`
      :profilePath;
    const body=input.storyId&&latest?{...payload,expected_revision:latest.revision}:payload;
    const created=await api<CareerStoryApiRow>(path,{method:'POST',body:JSON.stringify(body)});
    const readback=await load();
    const story=readback.find(item=>item.storyId===created.story_key);
    const revision=story?.revisions.find(item=>item.revisionId===created.story_id);
    if(!story||!revision||revision.reviewStatus!=='PENDING'||revision.reviewedAt!==null) {
      throw new Error('The new pending story revision was not confirmed by profile readback.');
    }
    return {storyId:story.storyId,revision};
  },[api,load,profilePath,stories]);

  const reviewRevision=useCallback(async(storyId:string,revisionId:string):Promise<CareerStoryRevision>=>{
    const story=stories.find(item=>item.storyId===storyId);
    const revision=story?.revisions.find(item=>item.revisionId===revisionId);
    if(!revision||!revision.isLatest||revision.reviewStatus!=='PENDING') {
      throw new Error('Only the current pending story revision can be reviewed. Reload the story history.');
    }
    const reviewed=await api<CareerStoryApiRow>(`${profilePath}/${encodeURIComponent(revisionId)}/review`,{
      method:'POST',body:JSON.stringify({approved:true,expected_revision:revision.revision}),
    });
    const readback=await load();
    const confirmed=readback.find(item=>item.storyId===storyId)?.revisions.find(item=>item.revisionId===reviewed.story_id);
    if(!confirmed||confirmed.reviewStatus!=='APPROVED'||!confirmed.reviewedAt) {
      throw new Error('The review decision was not confirmed by profile readback.');
    }
    return confirmed;
  },[api,load,profilePath,stories]);

  return <section className="panel" aria-label="Connected career story bank">
    <h2>Career story bank</h2>
    <p>Save user-authored STAR stories linked to verified, user-approved facts. Each revision stays unavailable for reuse until you review that exact revision.</p>
    {loading&&<p role="status">Loading career story history…</p>}
    {error&&<div role="alert"><p>{error}</p><button type="button" disabled={loading} onClick={()=>void load().catch(()=>undefined)}>Reload career stories</button></div>}
    {!loading&&!error&&<CareerStoryBank
      key={profileId}
      stories={stories}
      approvedFacts={toCareerStoryBankRows(facts)}
      onCreateRevision={createRevision}
      onReviewRevision={reviewRevision}
    />}
  </section>;
}
