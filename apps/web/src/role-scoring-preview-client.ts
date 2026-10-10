/** Read-only draft-ranking transport with scope, version and score-shape checks. */
import type {RoleScoringProfileDraft} from './role-scoring-profile-client';
import type {MatchExplanationContribution} from './matching-explanation';

export type PreviewJob={job_id:string;title:string;current_score:number;draft_score:number;delta:number;
  current_rank:number;draft_rank:number;draft_contributions:MatchExplanationContribution[]};
export type ScoringPreview={schema_version:1;read_only:true;profile_id:string;search_profile_id:string;
  profile_version:string;current_version:string|null;draft_id:string;snapshot_id:string;candidate_limit:number;
  scanned_count:number;eligible_count:number;skipped_count:number;truncated:boolean;jobs:PreviewJob[];limitation:string};
export type PreviewRequest={profileId:string;searchProfileId:string;draft:RoleScoringProfileDraft;
  expectedVersion:string|null;signal?:AbortSignal};
export type PreviewClient=(request:PreviewRequest)=>Promise<ScoringPreview>;

const record=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
const text=(v:unknown,max=200):v is string=>typeof v==='string'&&!!v.trim()&&v.length<=max;
const score=(v:unknown):v is number=>typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=100;
const integer=(v:unknown,max=100):v is number=>typeof v==='number'&&Number.isInteger(v)&&v>=0&&v<=max;
const hash=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const invalid=()=>new Error('The preview response could not be verified. No changes were saved.');

export function validatePreview(value:unknown,request:PreviewRequest):ScoringPreview {
  if(!record(value)||value.schema_version!==1||value.read_only!==true
    ||value.profile_id!==request.profileId||value.search_profile_id!==request.searchProfileId
    ||value.current_version!==request.expectedVersion||!text(value.profile_version)
    ||!hash(value.draft_id)||!hash(value.snapshot_id)||value.candidate_limit!==100
    ||!integer(value.scanned_count)||!integer(value.eligible_count)||!integer(value.skipped_count)
    ||value.eligible_count+value.skipped_count>value.scanned_count
    ||typeof value.truncated!=='boolean'||!text(value.limitation,2000)
    ||!Array.isArray(value.jobs)||value.jobs.length!==value.eligible_count) throw invalid();
  const expectedWeights:Record<string,number>={skills:100,...request.draft.weights};
  const expectedDimensions=Object.keys(expectedWeights).filter(key=>expectedWeights[key]>0);
  const ids=new Set<string>();
  const currentRanks=new Set<number>();
  for(const [index,row] of value.jobs.entries()) {
    if(!record(row)||!text(row.job_id)||ids.has(row.job_id)||!text(row.title,1000)
      ||!score(row.current_score)||!score(row.draft_score)||typeof row.delta!=='number'
      ||!Number.isFinite(row.delta)||Math.abs(row.delta-(row.draft_score-row.current_score))>0.011
      ||!integer(row.current_rank,value.eligible_count)||row.current_rank<1||currentRanks.has(row.current_rank)
      ||row.draft_rank!==index+1||!Array.isArray(row.draft_contributions)||row.draft_contributions.length>33) throw invalid();
    ids.add(row.job_id);currentRanks.add(row.current_rank);
    const dims=new Set<string>();
    let known=0,total=0,activeWeight=0,weightedPoints=0;
    for(const item of row.draft_contributions) {
      if(!record(item)||!text(item.dimension,100)||dims.has(item.dimension)||!score(item.weight)||item.weight===0
        ||!text(item.explanation,4000)||!record(item.provenance)||!text(item.provenance.source,128)
        ||(item.provenance.evidence_ref!==undefined&&!text(item.provenance.evidence_ref,2000))) throw invalid();
      if(item.weight!==expectedWeights[item.dimension]) throw invalid();
      const unknown=item.raw_score===null&&item.weighted_contribution===null;
      if(unknown?item.provenance.source!=='UNKNOWN':
        (!score(item.raw_score)||!score(item.weighted_contribution)||item.provenance.source==='UNKNOWN')) throw invalid();
      if(!unknown) {
        known++;total+=item.weighted_contribution as number;activeWeight+=item.weight;
        const effective=(item.raw_score as number)<(request.draft.thresholds[item.dimension]??0)?0:item.raw_score as number;
        weightedPoints+=item.weight*effective;
      }
      dims.add(item.dimension);
    }
    if(dims.size!==expectedDimensions.length||expectedDimensions.some(key=>!dims.has(key))) throw invalid();
    if(known&&(Math.abs(total-row.draft_score)>0.006*(known+1)
      ||Math.abs(weightedPoints/activeWeight-row.draft_score)>0.011)) throw invalid();
    if(activeWeight) for(const item of row.draft_contributions) {
      if(item.raw_score===null) continue;
      const effective=item.raw_score<(request.draft.thresholds[item.dimension]??0)?0:item.raw_score;
      if(Math.abs(item.weight*effective/activeWeight-item.weighted_contribution)>0.011) throw invalid();
    }
    if(index>0) {
      const previous=value.jobs[index-1];
      if(previous.draft_score<row.draft_score
        ||(previous.draft_score===row.draft_score&&previous.job_id>row.job_id)) throw invalid();
    }
  }
  const currentOrder=[...value.jobs].sort((left,right)=>right.current_score-left.current_score
    ||(left.job_id<right.job_id?-1:left.job_id>right.job_id?1:0));
  if(currentOrder.some((row,index)=>row.current_rank!==index+1)) throw invalid();
  return value as ScoringPreview;
}

export function createScoringPreviewClient(fetchImpl?:typeof fetch):PreviewClient {
  return async request=>{
    const {profileId,searchProfileId,draft,expectedVersion,signal}=request;
    for(const id of [profileId,searchProfileId]) {
      if(!text(id,128)||id!==id.trim()||id==='.'||id==='..'||/[\\/\u0000-\u001f\u007f]/.test(id)) throw invalid();
    }
    let response:Response;
    try {
      response=await (fetchImpl??fetch)(`/profiles/${encodeURIComponent(profileId)}/search-profiles/${encodeURIComponent(searchProfileId)}/role-scoring-profile/preview`,{
        method:'POST',headers:{accept:'application/json','content-type':'application/json'},signal,
        body:JSON.stringify({expected_version:expectedVersion,profile:draft}),
      });
    } catch(error) {
      if(signal?.aborted) throw error;
      throw new Error('The ranking preview could not be reached. Retry when connected; nothing was saved.');
    }
    if(!response.ok) {
      let code='';
      try {const body=await response.json();code=body?.detail?.code??'';} catch { /* Never echo raw responses. */ }
      if(code==='SCORING_PREVIEW_FILTERS_UNSUPPORTED') throw new Error('This preview does not support the saved advanced workspace filters. Use a separate search without those filters; existing filters were not changed.');
      if(code==='SCORING_PREVIEW_VERSION_CONFLICT') throw new Error('The saved weights changed. Reload the saved profile before previewing again; your draft was not saved.');
      throw new Error('The ranking preview was unavailable or rejected. Check the selected profile, search and draft; nothing was saved.');
    }
    let body:unknown;
    try {body=await response.json();} catch {throw invalid();}
    return validatePreview(body,request);
  };
}
