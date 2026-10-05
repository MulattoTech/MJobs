import { describe, expect, it } from 'vitest';
import { eligible } from './api';
import { updateReviewedFact } from './fact-review';

const fact = {
  id: 'stable-editor-id', fact_id: 'stable-fact-id', kind: 'skill', text: 'Python',
  source: 'Synthetic approved evidence', confidence: 100, verified: true, user_approved: true,
};

describe('fact-editor review boundary', () => {
  it.each([
    { text: 'Python and SQL' }, { source: 'Different evidence' }, { kind: 'technology_experience' },
    { text: '' }, { source: '' }, { text: 'Python ' },
  ])('invalidates both approvals for actual substantive edit %j', change => {
    const result = updateReviewedFact(fact, change);
    expect(result.verified).toBe(false);
    expect(result.user_approved).toBe(false);
    expect(eligible(result)).toBe(false);
    expect(result.id).toBe(fact.id);
    expect(result.fact_id).toBe(fact.fact_id);
    expect(result.confidence).toBe(fact.confidence);
  });
  it.each([{ text: fact.text }, { source: fact.source }, { kind: fact.kind }, {}])(
    'preserves flags for a no-op %j', change => expect(updateReviewedFact(fact, change)).toEqual(fact),
  );
  it('preserves confidence-only edits while retaining the existing eligibility threshold', () => {
    const low = updateReviewedFact(fact, { confidence: 79 });
    expect(low.verified).toBe(true);
    expect(low.user_approved).toBe(true);
    expect(eligible(low)).toBe(false);
    expect(eligible(updateReviewedFact(low, { confidence: 80 }))).toBe(true);
  });
  it('requires deliberate second approval after verification and does not auto-approve a revert', () => {
    const edited = updateReviewedFact(fact, { text: 'SQL' });
    const reverted = updateReviewedFact(edited, { text: fact.text });
    expect(eligible(reverted)).toBe(false);
    const verified = updateReviewedFact(reverted, { verified: true });
    expect(verified.user_approved).toBe(false);
    expect(eligible(verified)).toBe(false);
    expect(eligible(updateReviewedFact(verified, { user_approved: true }))).toBe(true);
  });
  it('gives substantive invalidation precedence over flags in the same edit', () => {
    const result = updateReviewedFact(fact, { text: 'SQL', verified: true, user_approved: true });
    expect(result.verified).toBe(false);
    expect(result.user_approved).toBe(false);
  });
  it('does not mutate existing facts or patch objects', () => {
    const original = Object.freeze({ ...fact });
    const change = Object.freeze({ text: 'SQL' });
    expect(updateReviewedFact(original, change).text).toBe('SQL');
    expect(original).toEqual(fact);
    expect(change).toEqual({ text: 'SQL' });
  });
});
