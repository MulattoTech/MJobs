import React from 'react';

export type ApplicationCapabilityFilterValue =
  | ''
  | 'FIXTURE_ONLY'
  | 'UNSUPPORTED'
  | 'REVIEW_REQUIRED'
  | 'UNKNOWN';

export type ApplicationCapabilityFilterProps = {
  value: ApplicationCapabilityFilterValue;
  onChange: (value: ApplicationCapabilityFilterValue) => void;
  disabled?: boolean;
};

export function ApplicationCapabilityFilter({
  value,
  onChange,
  disabled = false,
}: ApplicationCapabilityFilterProps) {
  return <div className="filter-control">
    <label htmlFor="application-capability">Application adapter capability (this workspace)</label>
    <select
      id="application-capability"
      aria-label="Application adapter capability (this workspace)"
      value={value}
      disabled={disabled}
      onChange={event => onChange(event.target.value as ApplicationCapabilityFilterValue)}
    >
      <option value="">All capabilities</option>
      <option value="FIXTURE_ONLY">Fixture-only support</option>
      <option value="UNSUPPORTED">Unsupported</option>
      <option value="REVIEW_REQUIRED">Review required</option>
      <option value="UNKNOWN">Unknown</option>
    </select>
    <small className="muted">
      Reports this workspace's existing adapter capability; it does not verify employer support. This filter makes no employer requests.
    </small>
  </div>;
}
