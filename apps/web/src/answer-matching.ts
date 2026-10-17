export type AnswerMatchMethod = 'EXACT_NORMALIZATION' | 'FUZZY' | 'CURATED_SEMANTIC';

export type AnswerQuestionMatch = {method:AnswerMatchMethod;score:number};

export type ReusableAnswerCandidate = {
  id:string;
  question?:string;
  text:string;
  source:string;
  confidence:number;
  verified:boolean;
  user_approved:boolean;
};

export type SuggestedAnswer = {
  answer:ReusableAnswerCandidate;
  match:AnswerQuestionMatch;
};

export type AnswerSuggestionPolicy = {
  minimum_saved_answer_confidence:number;
  minimum_question_match_confidence:number;
};

const sensitiveQuestion = /\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|date of birth|birth date|age|marital|citizenship|citizen|authoriz\w*|work authorization|eligible to work|work eligibility|right to work|visa|sponsorship|criminal|convict\w*|felon\w*|background check|salary expectation\w*|salary range|desired compensation|compensation expectation\w*|non[ -]?compete|conflict\w* of interest|minimum age|attest\w*|certif\w* under penalty)\b/i;
const questionNegation = /\b(?:no|not|never|without|neither|nor|unavailable|cannot|can['’]t|won['’]t|wouldn['’]t|shouldn['’]t|couldn['’]t|didn['’]t|doesn['’]t|don['’]t|isn['’]t|aren['’]t|wasn['’]t|weren['’]t|haven['’]t|hasn['’]t|hadn['’]t|mustn['’]t)\b/i;
const privateOrLegalQuestion = /\b(?:national origin|military service|served in the armed forces|social security(?: number)?|ssn|national id|arrest\w*|expected salary|expected compensation|salary requirements?|restrictive covenants?|legal certifications?|(?:at least |over |under )?\d{1,3}\s*(?:years?\s*(?:of age|old|or older)|\+\s*years?)|permi(?:tted|ssion) to work|allow\w* to work|medical\w*|health(?:care)?\s+(?:condition|status|history)|diagnos\w*|illness\w*|disease\w*|medication\w*|treatment\w*)\b/i;

export const isSensitiveAnswerQuestion = (value:string) => sensitiveQuestion.test(value)||privateOrLegalQuestion.test(value);
const isNegatedQuestion = (value:string) => questionNegation.test(value);

export const normalizeAnswerQuestion = (value:string) => value.toLowerCase().match(/[a-z0-9]+/g)?.join(' ')||'';

const semanticQuestionKey = (value:string) => {
  const normalized=normalizeAnswerQuestion(value);
  // This is a deliberately exact, ordinary availability paraphrase. Do not
  // generalize it to adjacent scheduling questions or infer an answer.
  if(normalized==='are you available to work weekends'||normalized==='can you work weekends'||normalized==='can you work on weekends')
    return 'can you work weekends';
  return normalizeAnswerQuestion(normalized
    .replace(/\b(coding|software development)\s+languages?\b/gi,'programming languages')
    // A narrow ordinary-question synonym; this is not a general semantic model.
    .replace(/\bcloud providers\b/gi,'cloud platforms')
    // Expand only the standard ITSM acronym, not adjacent service-desk wording.
    .replace(/\bitsm\b/gi,'it service management')
    .replace(/\b(what|which)\b/gi,'which')
    .replace(/\bworked with\b/gi,'used'));
};

const tokenEditDistance = (left:string[],right:string[]) => {
  let previous=Array.from({length:right.length+1},(_,index)=>index);
  for(let row=1;row<=left.length;row++) {
    const current=[row];
    for(let column=1;column<=right.length;column++) {
      current.push(Math.min(
        current[current.length-1]+1,
        previous[column]+1,
        previous[column-1]+(left[row-1]===right[column-1]?0:1),
      ));
    }
    previous=current;
  }
  return previous[previous.length-1];
};

export function matchAnswerQuestion(question:string,savedQuestion:string):AnswerQuestionMatch|null {
  if(isSensitiveAnswerQuestion(question)||isSensitiveAnswerQuestion(savedQuestion))return null;
  const normalized=normalizeAnswerQuestion(question);
  const saved=normalizeAnswerQuestion(savedQuestion);
  if(!normalized||!saved)return null;
  // Edit distance can hide a one-token polarity reversal behind a high score.
  if(isNegatedQuestion(question)!==isNegatedQuestion(savedQuestion))return null;
  if(normalized===saved)return {method:'EXACT_NORMALIZATION',score:1};
  if(semanticQuestionKey(question)===semanticQuestionKey(savedQuestion))
    return {method:'CURATED_SEMANTIC',score:0.95};

  const left=normalized.split(' ');
  const right=saved.split(' ');
  if(Math.max(left.length,right.length)>40||Math.min(left.length,right.length)<5)return null;
  const leftWords=new Set(left),rightWords=new Set(right);
  let overlap=0;
  for(const word of leftWords)if(rightWords.has(word))overlap++;
  const union=new Set([...leftWords,...rightWords]).size;
  const score=1-tokenEditDistance(left,right)/Math.max(left.length,right.length);
  if(overlap/union<0.7||score<0.75)return null;
  return {method:'FUZZY',score:Number(score.toFixed(3))};
}

export function suggestAnswers(question:string,answers:ReusableAnswerCandidate[],policy:AnswerSuggestionPolicy={
  minimum_saved_answer_confidence:80,minimum_question_match_confidence:0.75,
}):SuggestedAnswer[] {
  if(isSensitiveAnswerQuestion(question))return [];
  const minimumConfidence=policy.minimum_saved_answer_confidence;
  const minimumMatch=policy.minimum_question_match_confidence;
  if(!Number.isInteger(minimumConfidence)||minimumConfidence<80||minimumConfidence>100||
     !Number.isFinite(minimumMatch)||minimumMatch<0.75||minimumMatch>1)return [];
  const matches=answers.flatMap(answer=>{
    if(!answer.question?.trim()||!answer.text.trim()||!answer.source.trim()||
       answer.verified!==true||answer.user_approved!==true||
       !Number.isFinite(answer.confidence)||answer.confidence<minimumConfidence||answer.confidence>100)return [];
    const match=matchAnswerQuestion(question,answer.question);
    return match&&match.score>=minimumMatch?[{answer,match}]:[];
  });
  // Different approved values require review even when the conflicting match
  // would fall outside the three suggestions shown by the legacy picker.
  if(new Set(matches.map(({answer})=>answer.text.trim().replace(/\s+/g,' '))).size>1)return [];
  return matches.sort((left,right)=>right.match.score-left.match.score||left.answer.id.localeCompare(right.answer.id)).slice(0,3);
}
