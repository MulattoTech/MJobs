import { describe, expect, it } from 'vitest';
import { matchCategory } from './match-category';

describe('matchCategory', () => {
  it.each([
    [0, 'Poor'],
    [38, 'Poor'],
    [39, 'Poor'],
    [40, 'Weak'],
    [41, 'Weak'],
    [59, 'Weak'],
    [60, 'Possible'],
    [61, 'Possible'],
    [74, 'Possible'],
    [75, 'Strong'],
    [76, 'Strong'],
    [84, 'Strong'],
    [85, 'Excellent'],
    [86, 'Excellent'],
    [94, 'Excellent'],
    [95, 'Exceptional'],
    [96, 'Exceptional'],
    [100, 'Exceptional'],
  ] as const)('categorizes score %i as %s', (score, category) => {
    expect(matchCategory(score)).toBe(category);
  });

  it('leaves an unscored job unclassified', () => {
    expect(matchCategory(null)).toBeNull();
  });

  it.each([-1, 101, Number.NaN, Number.POSITIVE_INFINITY])(
    'leaves invalid score %s unclassified', score => {
      expect(matchCategory(score)).toBeNull();
    },
  );
});
