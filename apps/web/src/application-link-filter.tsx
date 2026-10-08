import React from 'react';

export type ApplicationLinkStatusFilter = '' | 'AVAILABLE' | 'UNAVAILABLE';

export type ApplicationLinkFilterProps = {
  value: ApplicationLinkStatusFilter;
  onChange: (value: ApplicationLinkStatusFilter) => void;
  disabled?: boolean;
};

export function ApplicationLinkFilter({
  value,
  onChange,
  disabled = false,
}: ApplicationLinkFilterProps) {
  return <div className="filter-control">
    <label htmlFor="application-link-availability">Application link availability</label>
    <select
      id="application-link-availability"
      aria-label="Application link availability"
      value={value}
      disabled={disabled}
      onChange={event => onChange(event.target.value as ApplicationLinkStatusFilter)}
    >
      <option value="">All availability</option>
      <option value="AVAILABLE">Available</option>
      <option value="UNAVAILABLE">Unavailable</option>
    </select>
    <small className="muted">
      Unavailable means no application link was reported. Unknown application link states are excluded when a filter is selected.
    </small>
  </div>;
}
