export type ReviewedFact = {
  text: string;
  source: string;
  kind: string;
  verified: boolean;
  user_approved: boolean;
};

/** Review belongs to the exact content, provenance and classification reviewed. */
export function updateReviewedFact<T extends ReviewedFact>(fact: T, change: Partial<T>): T {
  const contentChanged = (['text', 'source', 'kind'] as const).some(
    key => Object.prototype.hasOwnProperty.call(change, key) && change[key] !== fact[key],
  );
  return {
    ...fact,
    ...change,
    ...(contentChanged ? { verified: false, user_approved: false } : {}),
  };
}
