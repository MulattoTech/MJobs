import {describe,expect,it} from 'vitest';
import {matchAnswerQuestion,suggestAnswers} from './answer-matching';

describe('application answer question matching',()=>{
  it('normalizes punctuation and case without upgrading the match method',()=>{
    expect(matchAnswerQuestion('How many years of experience?','HOW MANY YEARS OF EXPERIENCE!'))
      .toEqual({method:'EXACT_NORMALIZATION',score:1});
  });

  it('labels a small question wording edit as a scored fuzzy match',()=>{
    expect(matchAnswerQuestion(
      'How many years of professional experience have you had?',
      'How many years of professional experience do you have?',
    )).toEqual({method:'FUZZY',score:0.778});
  });

  it('labels only the explicit programming-language synonym set as curated semantic',()=>{
    expect(matchAnswerQuestion(
      'What coding languages have you worked with?',
      'Which programming languages have you used?',
    )).toEqual({method:'CURATED_SEMANTIC',score:0.95});
  });

  it('rejects a near miss and every sensitive or legal prompt',()=>{
    expect(matchAnswerQuestion(
      'How many years of education?',
      'How many years of professional experience do you have?',
    )).toBeNull();
    expect(matchAnswerQuestion(
      'Are you legally authorized for employment in the United States?',
      'Are you authorized to work in the US?',
    )).toBeNull();
  });

  it('suggests only eligible answers and ranks exact matches before weaker matches',()=>{
    const bank=[
      {id:'fuzzy',question:'How many years of professional experience do you have?',text:'seven years',source:'synthetic profile',confidence:91,verified:true,user_approved:true},
      {id:'exact',question:'How many years of professional experience have you had?',text:'seven years',source:'synthetic profile',confidence:88,verified:true,user_approved:true},
      {id:'unverified',question:'How many years of professional experience have you had?',text:'unreviewed',source:'synthetic profile',confidence:99,verified:false,user_approved:true},
      {id:'low-confidence',question:'How many years of professional experience have you had?',text:'uncertain',source:'synthetic profile',confidence:79,verified:true,user_approved:true},
    ];
    const suggestions=suggestAnswers('How many years of professional experience have you had?',bank);
    expect(suggestions.map(item=>[item.answer.id,item.match.method,item.match.score])).toEqual([
      ['exact','EXACT_NORMALIZATION',1],
      ['fuzzy','FUZZY',0.778],
    ]);
    expect(suggestAnswers('Are you authorized to work in the US?',bank)).toEqual([]);
  });
});
