export type MatchCategory = 'Exceptional' | 'Excellent' | 'Strong' | 'Possible' | 'Weak' | 'Poor';

export function matchCategory(score: number | null): MatchCategory | null {
  if (score === null || !Number.isFinite(score) || score < 0 || score > 100) return null;
  if (score >= 95) return 'Exceptional';
  if (score >= 85) return 'Excellent';
  if (score >= 75) return 'Strong';
  if (score >= 60) return 'Possible';
  if (score >= 40) return 'Weak';
  return 'Poor';
}
