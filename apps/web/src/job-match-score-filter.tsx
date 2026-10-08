import React from 'react';

export type JobMatchScoreFilterProps = {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
};

export function JobMatchScoreFilter({ value, onChange, disabled = false }: JobMatchScoreFilterProps) {
  return <>
    <label>Minimum match score (%)<input aria-label="Minimum match score (%)" type="number"
      min="0" max="100" step="1" value={value} disabled={disabled}
      onChange={event => onChange(event.target.value)} /></label>
    <small className="muted">Unscored jobs are excluded while a minimum score is set.</small>
  </>;
}
