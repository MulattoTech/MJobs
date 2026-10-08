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

const sensitiveQuestion = /\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|date of birth|birth date|age|marital|citizenship|citizen|authoriz\w*|work authorization|eligible to work|work eligibility|right to work|visa|sponsorship|criminal|convict\w*|felon\w*|background check|salary expectation\w*|salary range|desired compensation|compensation expectation\w*|non[ -]?compete|conflict\w* of interest|minimum age|attest\w*|certif\w* under penalty)\b/i;

export const isSensitiveAnswerQuestion = (value:string) => sensitiveQuestion.test(value);

export const normalizeAnswerQuestion = (value:string) => value.toLowerCase().match(/[a-z0-9]+/g)?.join(' ')||'';

const semanticQuestionKey = (value:string) => normalizeAnswerQuestion(value
  .replace(/\b(coding|software development)\s+languages?\b/gi,'programming languages')
  // A narrow ordinary-question synonym; this is not a general semantic model.
  .replace(/\bcloud providers\b/gi,'cloud platforms')
  // Expand only the standard ITSM acronym, not adjacent service-desk wording.
  .replace(/\bitsm\b/gi,'it service management')
  .replace(/\b(what|which)\b/gi,'which')
  .replace(/\bworked with\b/gi,'used'));

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

export function suggestAnswers(question:string,answers:ReusableAnswerCandidate[]):SuggestedAnswer[] {
  if(isSensitiveAnswerQuestion(question))return [];
  return answers.flatMap(answer=>{
    if(!answer.question?.trim()||!answer.text.trim()||!answer.source.trim()||
       answer.verified!==true||answer.user_approved!==true||
       !Number.isFinite(answer.confidence)||answer.confidence<80)return [];
    const match=matchAnswerQuestion(question,answer.question);
    return match?[{answer,match}]:[];
  }).sort((left,right)=>right.match.score-left.match.score||left.answer.id.localeCompare(right.answer.id)).slice(0,3);
}
