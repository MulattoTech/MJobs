import React from 'react';

export type SalaryPeriod = 'YEAR' | 'HOUR';
export type SalaryPolicy = 'DEFAULT' | 'KNOWN_ONLY' | 'ALLOW_UNKNOWN';

export type SalarySelection = {
  min: string;
  max: string;
  period: SalaryPeriod;
  policy: SalaryPolicy;
};

export const DEFAULT_SALARY_SELECTION: SalarySelection = {
  min: '',
  max: '',
  period: 'YEAR',
  policy: 'DEFAULT',
};

function validBound(value: string | null): string {
  if (!value || value.length > 32 || !/^\d+(?:\.\d{1,2})?$/.test(value)) return '';
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 && amount <= 100000000 ? value : '';
}

/** Only the named salary controls are restored; unrelated URL state is preserved. */
export function readSalarySelection(search: string): SalarySelection {
  const params = new URLSearchParams(search);
  const period = params.get('salary_period');
  const policy = params.get('salary_policy');
  return {
    min: validBound(params.get('salary_min')),
    max: validBound(params.get('salary_max')),
    period: period === 'HOUR' ? 'HOUR' : 'YEAR',
    policy: policy === 'KNOWN_ONLY' || policy === 'ALLOW_UNKNOWN' ? policy : 'DEFAULT',
  };
}

/** The API does not receive the browser-only salary_policy display value. */
export function applySalarySearchParams(params: URLSearchParams, selection: SalarySelection): void {
  if (selection.min) params.set('salary_min', selection.min);
  if (selection.max) params.set('salary_max', selection.max);
  if (selection.min || selection.max || selection.policy !== 'DEFAULT') {
    params.set('salary_period', selection.period);
    if (selection.policy === 'KNOWN_ONLY') params.set('salary_known_only', 'true');
    if (selection.policy === 'ALLOW_UNKNOWN') params.set('salary_unknown_allowed', 'true');
  }
}

/** Produce a reloadable URL without adding one browser-history entry per keystroke. */
export function salarySearchUrl(href: string, selection: SalarySelection): string {
  const url = new URL(href);
  for (const key of ['salary_min', 'salary_max', 'salary_period', 'salary_policy']) {
    url.searchParams.delete(key);
  }
  if (validBound(selection.min)) url.searchParams.set('salary_min', selection.min);
  if (validBound(selection.max)) url.searchParams.set('salary_max', selection.max);
  if (selection.period !== 'YEAR' || selection.min || selection.max || selection.policy !== 'DEFAULT') {
    url.searchParams.set('salary_period', selection.period);
  }
  if (selection.policy !== 'DEFAULT') url.searchParams.set('salary_policy', selection.policy);
  return url.pathname + url.search + url.hash;
}

type Props = SalarySelection & {
  disabled?: boolean;
  onMinChange: (value: string) => void;
  onMaxChange: (value: string) => void;
  onPeriodChange: (value: SalaryPeriod) => void;
  onPolicyChange: (value: SalaryPolicy) => void;
};

export function SalarySearchControls({
  min,
  max,
  period,
  policy,
  disabled = false,
  onMinChange,
  onMaxChange,
  onPeriodChange,
  onPolicyChange,
}: Props) {
  const unit = period === 'YEAR' ? 'annual' : 'hourly';
  const periodLabel = period === 'YEAR' ? 'year' : 'hour';
  return (
    <>
      <label>
        Salary unit
        <select aria-label="Salary unit" value={period} disabled={disabled}
          onChange={event => onPeriodChange(event.target.value as SalaryPeriod)}>
          <option value="YEAR">Annual (USD/year)</option>
          <option value="HOUR">Hourly (USD/hour)</option>
        </select>
      </label>
      <label>
        Salary availability
        <select aria-label="Salary availability" value={policy} disabled={disabled}
          onChange={event => onPolicyChange(event.target.value as SalaryPolicy)}>
          <option value="DEFAULT">Default: all jobs without bounds</option>
          <option value="KNOWN_ONLY">Require disclosed USD salary</option>
          <option value="ALLOW_UNKNOWN">Also show jobs with no disclosed salary</option>
        </select>
      </label>
      <label>
        Minimum {unit} salary (USD)
        <input aria-label={`Minimum ${unit} salary (USD)`} type="number" min="0" max="100000000"
          step="0.01" value={min} disabled={disabled} onChange={event => onMinChange(event.target.value)} />
      </label>
      <label>
        Maximum {unit} salary (USD)
        <input aria-label={`Maximum ${unit} salary (USD)`} type="number" min="0" max="100000000"
          step="0.01" value={max} disabled={disabled} onChange={event => onMaxChange(event.target.value)} />
      </label>
      <small className="muted">
        Bounds are USD per {periodLabel}, never converted between pay periods. Missing salary is excluded
        while bounds are set unless explicitly allowed. Non-USD pay, incompatible periods, malformed ranges
        and estimates do not satisfy bounds. A missing range endpoint cannot prove the opposite bound.
        Changing salary unit clears entered bounds. Salary sorting still compares annual USD only.
      </small>
    </>
  );
}
