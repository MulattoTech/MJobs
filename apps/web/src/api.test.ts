import { describe, expect, it } from 'vitest';
import { eligible, terminal } from './api';
describe('user safety presentation', () => {
  it('keeps pending, low confidence and unsourced facts out of document eligibility', () => {
    const fact = { verified: true, user_approved: true, confidence: 100, source: 'My records' };
    expect(eligible(fact)).toBe(true);
    for (const change of [{verified:false}, {user_approved:false}, {confidence:79}, {source:'  '}]) expect(eligible({...fact, ...change})).toBe(false);
  });
  it('stops polling failed, blocked and cancelled work', () => {
    for (const state of ['COMPLETED','FAILED','BLOCKED','CANCELLED']) expect(terminal.has(state)).toBe(true);
    expect(terminal.has('QUEUED')).toBe(false);
  });
});
