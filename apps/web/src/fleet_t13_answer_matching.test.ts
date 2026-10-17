import {describe, expect, it} from 'vitest';
import {suggestAnswers, type ReusableAnswerCandidate} from './answer-matching';

const question = 'Can you work weekends?';
const saved = (id: string, text: string, confidence = 100): ReusableAnswerCandidate => ({
  id, question, text, confidence, source: 'Synthetic user entry', verified: true, user_approved: true,
});

describe('Team 13 legacy answer review gates', () => {
  it('withholds conflicting eligible answers rather than selecting a ranked value', () => {
    expect(suggestAnswers(question, [saved('yes', 'Yes'), saved('no', 'No')])).toEqual([]);
  });

  it('applies the configured policy before evaluating conflicts', () => {
    expect(suggestAnswers(question, [saved('high', 'Yes'), saved('low', 'No', 90)], {
      minimum_saved_answer_confidence: 95, minimum_question_match_confidence: 0.75,
    }).map(row => row.answer.id)).toEqual(['high']);
    expect(suggestAnswers('Are you available to work weekends?', [saved('exact', 'Yes')], {
      minimum_saved_answer_confidence: 80, minimum_question_match_confidence: 1,
    })).toEqual([]);
  });

  it('checks conflicts before truncating the ranked candidate list', () => {
    expect(suggestAnswers(question, [saved('a', 'Yes'), saved('b', 'Yes'),
      saved('c', 'Yes'), saved('z', 'No')])).toEqual([]);
  });

  it('retains aligned rank and ignores unapproved conflicts', () => {
    const paraphrase = {...saved('semantic', 'Yes'), question: 'Are you available to work weekends?'};
    const unapproved = {...saved('unapproved', 'No'), user_approved: false};
    expect(suggestAnswers(question, [paraphrase, saved('exact', ' Yes '), unapproved])
      .map(row => [row.answer.id, row.match.method])).toEqual([
      ['exact', 'EXACT_NORMALIZATION'], ['semantic', 'CURATED_SEMANTIC'],
    ]);
  });

  it.each(['What is your social security number?', 'What is your national origin?',
    'Have you been arrested?', 'Are you 18 years or older?', 'What is your expected salary?',
    'Do you have a medical condition?'])('withholds known sensitive or legal wording: %s', label => {
    expect(suggestAnswers(label, [{...saved('private', 'Synthetic private response'), question: label}])).toEqual([]);
  });
});
