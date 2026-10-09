import {isSensitiveAnswerQuestion} from './answer-matching';

export type ApplicationAnswerMatchMethod='EXACT_NORMALIZATION'|'FUZZY'|'CURATED_SEMANTIC';

export type ApplicationAnswerMatchScope={profileId:string;applicationId:string;question:string};

export type ApplicationAnswerMatchCandidate={
  answer_id:string;
  question:string;
  text:string;
  source:string;
  confidence:number;
  verified:boolean;
  user_approved:boolean;
  method:ApplicationAnswerMatchMethod;
  score:number;
  explanation:string;
};

export type ApplicationAnswerStoryFactProvenance={fact_id:string;fingerprint:string};

/** User-authored source material kept separate from answer suggestion values. */
export type ApplicationAnswerStoryReference={
  story_id:string;
  revision:number;
  current_revision:number;
  is_current:true;
  title:string;
  text:string;
  allowed_use:'APPLICATION_ANSWER';
  fact_provenance:ApplicationAnswerStoryFactProvenance[];
  review_status:'APPROVED';
};

export type ApplicationAnswerMatchResponse={
  scope:ApplicationAnswerMatchScope;
  profile_id:string;
  application_id:string;
  question:string;
  candidates:ApplicationAnswerMatchCandidate[];
  /** Optional only for existing view fixtures; the network client requires this field. */
  story_references?:ApplicationAnswerStoryReference[];
};

export type LoadedApplicationAnswerMatchResponse=ApplicationAnswerMatchResponse & {
  story_references:ApplicationAnswerStoryReference[];
};

export type ApplicationAnswerSuggestionDraft={
  answer_id:string;
  text:string;
  profile_id:string;
  application_id:string;
  question:string;
  reviewed:false;
  method:ApplicationAnswerMatchMethod;
  score:number;
  saved_question:string;
  source:string;
  confidence:number;
  verified:boolean;
  user_approved:boolean;
  explanation:string;
};

export type ApplicationAnswerSelectionResult=
  | {ok:true;draft:ApplicationAnswerSuggestionDraft}
  | {ok:false;reason:'STALE_SCOPE'|'SENSITIVE_QUESTION'|'BELOW_THRESHOLD'|'INVALID_CANDIDATE'};

export type ApplicationAnswerSelectionThresholds={minimumConfidence?:number;minimumMatchScore?:number};

export type AnswerMatchClientErrorCode=
  'INVALID_REQUEST'|'NETWORK_ERROR'|'REQUEST_FAILED'|'INVALID_RESPONSE'|'STALE_SCOPE'|'SENSITIVE_QUESTION';

export class AnswerMatchClientError extends Error {
  constructor(readonly code:AnswerMatchClientErrorCode,message:string) {
    super(message);
    this.name='AnswerMatchClientError';
  }
}

const maxCandidates=3;
const methods=new Set<ApplicationAnswerMatchMethod>(['EXACT_NORMALIZATION','FUZZY','CURATED_SEMANTIC']);
const invalidResponse=()=>new AnswerMatchClientError('INVALID_RESPONSE','The answer suggestion service returned an invalid response.');
const staleScope=()=>new AnswerMatchClientError('STALE_SCOPE','This suggestion request belongs to an earlier application question.');
const sameScope=(left:ApplicationAnswerMatchScope,right:ApplicationAnswerMatchScope)=>
  left.profileId===right.profileId&&left.applicationId===right.applicationId&&left.question===right.question;

const isRecord=(value:unknown):value is Record<string,unknown>=>typeof value==='object'&&value!==null&&!Array.isArray(value);
const isText=(value:unknown):value is string=>typeof value==='string'&&value.trim().length>0;

const isCandidate=(value:unknown):value is ApplicationAnswerMatchCandidate=>{
  if(!isRecord(value))return false;
  return isText(value.answer_id)&&typeof value.method==='string'&&methods.has(value.method as ApplicationAnswerMatchMethod)&&
    typeof value.score==='number'&&Number.isFinite(value.score)&&value.score>=0&&value.score<=1&&
    isText(value.question)&&isText(value.text)&&isText(value.source)&&isText(value.explanation)&&
    typeof value.confidence==='number'&&Number.isFinite(value.confidence)&&value.confidence>=0&&value.confidence<=100&&
    typeof value.verified==='boolean'&&typeof value.user_approved==='boolean';
};

const isStoryFactProvenance=(value:unknown):value is ApplicationAnswerStoryFactProvenance=>{
  if(!isRecord(value))return false;
  return isText(value.fact_id)&&isText(value.fingerprint);
};

const isStoryReference=(value:unknown):value is ApplicationAnswerStoryReference=>{
  if(!isRecord(value)||!isText(value.story_id)||!Number.isSafeInteger(value.revision)||
     (value.revision as number)<1||!Number.isSafeInteger(value.current_revision)||
     value.current_revision!==value.revision||value.is_current!==true||
     !isText(value.title)||!isText(value.text)||
     value.allowed_use!=='APPLICATION_ANSWER'||value.review_status!=='APPROVED'||
     !Array.isArray(value.fact_provenance)||value.fact_provenance.length===0||
     !value.fact_provenance.every(isStoryFactProvenance))return false;
  const factIds=value.fact_provenance.map((row)=>row.fact_id);
  return new Set(factIds).size===factIds.length;
};

const isStoryReferenceList=(value:unknown):value is ApplicationAnswerStoryReference[]=>{
  if(!Array.isArray(value)||!value.every(isStoryReference))return false;
  const storyIds=value.map((row)=>row.story_id);
  return new Set(storyIds).size===storyIds.length;
};

function requestFailureMessage(status:number):string {
  if(status===404)return 'This profile or application is no longer available.';
  if(status===400||status===422)return 'The question could not be matched. Check it and try again.';
  return 'Application answer suggestions are temporarily unavailable.';
}

/** Fetches advisory candidates only; this never selects, saves, approves, exports, or fills an answer. */
export async function matchApplicationAnswers(
  scope:ApplicationAnswerMatchScope,
  getActiveScope:()=>ApplicationAnswerMatchScope,
  fetcher:typeof fetch=globalThis.fetch,
):Promise<LoadedApplicationAnswerMatchResponse> {
  if(!isText(scope?.profileId)||!isText(scope?.applicationId)||!isText(scope?.question))
    throw new AnswerMatchClientError('INVALID_REQUEST','Choose an application and enter a question before requesting suggestions.');
  if(isSensitiveAnswerQuestion(scope.question))
    throw new AnswerMatchClientError('SENSITIVE_QUESTION','Sensitive or legally significant questions do not receive reusable answer suggestions.');

  const assertCurrentScope=()=>{
    let activeScope:ApplicationAnswerMatchScope;
    try { activeScope=getActiveScope(); }
    catch { throw staleScope(); }
    if(!activeScope||!sameScope(scope,activeScope))throw staleScope();
  };
  assertCurrentScope();

  let response:Response;
  try {
    response=await fetcher(
      `/profiles/${encodeURIComponent(scope.profileId)}/applications/${encodeURIComponent(scope.applicationId)}/answers/match`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:scope.question})},
    );
  } catch {
    assertCurrentScope();
    throw new AnswerMatchClientError('NETWORK_ERROR','Could not reach application answer matching. Try again.');
  }
  assertCurrentScope();

  if(!response.ok)throw new AnswerMatchClientError('REQUEST_FAILED',requestFailureMessage(response.status));

  let payload:unknown;
  try { payload=await response.json(); }
  catch {
    assertCurrentScope();
    throw invalidResponse();
  }
  assertCurrentScope();

  if(!isRecord(payload)||!isText(payload.profile_id)||!isText(payload.application_id)||!isText(payload.question))
    throw invalidResponse();
  if(payload.profile_id!==scope.profileId||payload.application_id!==scope.applicationId||payload.question!==scope.question)
    throw staleScope();
  if(!Array.isArray(payload.candidates)||payload.candidates.length>maxCandidates||!payload.candidates.every(isCandidate))
    throw invalidResponse();
  if(!isStoryReferenceList(payload.story_references))throw invalidResponse();

  assertCurrentScope();
  return {
    scope:{...scope},
    profile_id:payload.profile_id,
    application_id:payload.application_id,
    question:payload.question,
    candidates:payload.candidates,
    story_references:payload.story_references,
  };
}

/** Explicit click handler helper: returns only a bound, unreviewed draft proposal. */
export function selectApplicationAnswerSuggestion(
  candidate:ApplicationAnswerMatchCandidate,
  responseScope:ApplicationAnswerMatchScope,
  activeScope:ApplicationAnswerMatchScope,
  thresholds:ApplicationAnswerSelectionThresholds={},
):ApplicationAnswerSelectionResult {
  if(!responseScope||!activeScope||!sameScope(responseScope,activeScope))return {ok:false,reason:'STALE_SCOPE'};
  if(isSensitiveAnswerQuestion(activeScope.question))return {ok:false,reason:'SENSITIVE_QUESTION'};
  if(!isCandidate(candidate))return {ok:false,reason:'INVALID_CANDIDATE'};
  const minimumConfidence=Math.max(80,thresholds.minimumConfidence??80);
  const minimumMatchScore=Math.max(0.75,thresholds.minimumMatchScore??0.75);
  if(!Number.isFinite(minimumConfidence)||minimumConfidence>100||
     !Number.isFinite(minimumMatchScore)||minimumMatchScore>1||
     !candidate.verified||!candidate.user_approved||candidate.confidence<minimumConfidence||candidate.score<minimumMatchScore)
    return {ok:false,reason:'BELOW_THRESHOLD'};

  return {ok:true,draft:{
    answer_id:candidate.answer_id,
    text:candidate.text,
    profile_id:responseScope.profileId,
    application_id:responseScope.applicationId,
    question:responseScope.question,
    reviewed:false,
    method:candidate.method,
    score:candidate.score,
    saved_question:candidate.question,
    source:candidate.source,
    confidence:candidate.confidence,
    verified:candidate.verified,
    user_approved:candidate.user_approved,
    explanation:candidate.explanation,
  }};
}
