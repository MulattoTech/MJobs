import React from 'react';

export type SalaryFields = {
  salary_min?: number | null;
  salary_max?: number | null;
  salary_currency?: string | null;
  salary_period?: string | null;
  salary_source?: string | null;
};
type LegacySalaryRange = {min: number | null; max: number | null; currency: string; period: string};
export type JobSalaryData = SalaryFields & {
  attributes?: SalaryFields & {salary?: LegacySalaryRange[]};
};

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function hasBounds(value: Record<string, unknown>): boolean {
  return Object.prototype.hasOwnProperty.call(value, 'salary_min')
    || Object.prototype.hasOwnProperty.call(value, 'salary_max');
}
function bound(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}
function range(min: unknown, max: unknown, currency: unknown, period: unknown): string | null {
  if ((min != null && !bound(min)) || (max != null && !bound(max))
      || (!bound(min) && !bound(max)) || (bound(min) && bound(max) && min > max)) return null;
  const number = (value: number) => value.toLocaleString('en-US', {maximumFractionDigits:20});
  const amount = bound(min) && bound(max)
    ? min === max ? number(min) : `${number(min)}–${number(max)}`
    : bound(min) ? `from ${number(min)}` : `up to ${number(max as number)}`;
  const unit = typeof currency === 'string' && /^[A-Z]{3}$/i.test(currency)
    ? currency.toUpperCase() : 'Currency not specified:';
  const periods: Record<string, string> = {
    HOUR:'per hour', YEAR:'per year', MONTH:'per month', WEEK:'per week', DAY:'per day',
  };
  const frequency = typeof period === 'string' ? periods[period.toUpperCase()] : undefined;
  return `${unit} ${amount} ${frequency || '(period not specified)'}`;
}
function provenance(source: unknown): string {
  if (source === 'ESTIMATED') return 'Estimated salary range (not employer-confirmed)';
  if (source === 'SOURCE_PROVIDED' || source === 'EMPLOYER_PROVIDED') {
    return 'Source-reported salary range (not independently verified)';
  }
  return 'Recorded salary range (provenance not specified)';
}

export function JobSalary({job}: {job: JobSalaryData}) {
  const canonical = record(job);
  const attributes = record(canonical.attributes);
  // Explicit canonical null/invalid values must not resurrect stale attributes
  // or legacy ranges. Never combine bounds or units from different snapshots.
  const scalar = hasBounds(canonical) ? canonical : hasBounds(attributes) ? attributes : null;
  const values = scalar
    ? [range(scalar.salary_min, scalar.salary_max, scalar.salary_currency, scalar.salary_period)]
    : Array.isArray(attributes.salary) ? attributes.salary.map(item => {
      const row = record(item);
      return range(row.min, row.max, row.currency, row.period);
    }) : [];
  const ranges = values.filter((value): value is string => value !== null);
  if (!ranges.length) return null;
  return <p aria-label="Salary range">{provenance(scalar ? scalar.salary_source : attributes.salary_source)}: {ranges.join('; ')}</p>;
}
